#!/usr/bin/env python3
"""Measure whether CodeGraph saves exploration in Claude Code sessions.

Reads this repo's Claude Code transcripts and reports, per session and per
A/B arm (see .claude/hooks/codegraph-hook.sh):

  lookups/p  code reads + searches per prompt (Read, Grep, Glob, and
             cat/sed/grep/rg... in Bash)
  reread     reads of a file the hook had already injected into context
  inj KB     CodeGraph context injected by the hook
  ctx M/p    context tokens (input + cache), millions per prompt
  cg         direct CodeGraph use (MCP tool or `codegraph` CLI)

The hook is worth keeping if "on" shows fewer lookups/prompt and fewer
context tokens/prompt than "off" over comparable work.

Usage: scripts/codegraph-eval.py [--since YYYY-MM-DD]
"""

import argparse
import collections
import json
import re
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECTS = Path.home() / ".claude" / "projects"
ARMS_LOG = REPO / ".codegraph" / "ab-arms.log"

CODE_PATH = re.compile(r"((?:[\w.-]+/)+[\w.-]+\.(?:py|vue|js|ts|tsx|sql))")
INJECTION = re.compile(r"<codegraph_context.*?</codegraph_context>", re.S)
SEARCH_CMDS = {"rg", "grep", "find", "fd", "ag", "git grep"}
READ_CMDS = {"cat", "sed", "head", "tail", "nl", "awk", "less", "bat"}
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def command_head(cmd: str) -> str:
    """First real command of a Bash call, skipping `cd` and env prefixes."""
    for seg in re.split(r"&&|;|\n", cmd):
        seg = seg.split("|")[0].strip()
        if not seg or seg.startswith("cd "):
            continue
        seg = re.sub(r"^(?:[A-Za-z_]+=\S+\s+)+", "", seg)
        toks = seg.split()
        if not toks:
            continue
        if toks[0] == "rtk" and len(toks) > 1:
            toks = toks[1:]
        if toks[0] == "git" and len(toks) > 1:
            return f"git {toks[1]}"
        return toks[0]
    return ""


def relative(path: str) -> str:
    return path.split(str(REPO) + "/")[-1]


def is_prompt(d: dict, content) -> bool:
    """A message the user typed, as opposed to a tool result or meta entry."""
    if d.get("type") != "user" or d.get("isMeta"):
        return False
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "text" for b in content
    )


def load_arms() -> dict[str, str]:
    arms: dict[str, str] = {}
    if ARMS_LOG.exists():
        for line in ARMS_LOG.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                arms[parts[0]] = parts[1]
    return arms


def analyse(transcript: Path) -> tuple[str, collections.Counter]:
    """Session start date and its counters."""
    s: collections.Counter = collections.Counter()
    shown: set[str] = set()
    first_ts = None
    for line in transcript.open():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        first_ts = first_ts or d.get("timestamp")
        # Hook output is recorded as an attachment; tool results that merely
        # mention the tag (e.g. testing the hook) must not count.
        if "codegraph_context" in line and d.get("type") == "attachment":
            for block in INJECTION.findall(line.replace("\\n", "\n")):
                s["inj_bytes"] += len(block)
                shown |= {relative(p) for p in CODE_PATH.findall(block)}
        msg = d.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage") or {}
        s["ctx"] += sum(usage.get(k, 0) for k in USAGE_KEYS)
        content = msg.get("content")
        if is_prompt(d, content):
            s["prompts"] += 1
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name, inp = b["name"], b.get("input", {})
            targets: list[str] = []
            if "codegraph" in name:
                s["cg"] += 1
            elif name == "Read":
                s["lookups"] += 1
                targets = [inp.get("file_path", "")]
            elif name in ("Grep", "Glob"):
                s["lookups"] += 1
            elif name == "Bash":
                cmd = inp.get("command", "")
                head = command_head(cmd)
                if head == "codegraph":
                    s["cg"] += 1
                elif head in SEARCH_CMDS:
                    s["lookups"] += 1
                elif head in READ_CMDS and not re.search(r"\bsed\s+-i", cmd):
                    s["lookups"] += 1
                    targets = CODE_PATH.findall(cmd)
            if shown and any(relative(t) in shown for t in targets if t):
                s["reread"] += 1
    return (first_ts or "?")[:10], s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help="only sessions starting on/after YYYY-MM-DD")
    args = ap.parse_args()

    slug = re.sub(r"[^A-Za-z0-9]", "-", str(REPO))
    transcripts = sorted((PROJECTS / slug).glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    arms = load_arms()

    rows = []
    for t in transcripts:
        date, s = analyse(t)
        if not s["prompts"] or (args.since and date < args.since):
            continue
        arm = arms.get(t.stem) or ("on?" if s["inj_bytes"] else "unlogged")
        rows.append((t.stem, arm, date, s))

    header = ("date", "session", "arm", "prompts", "lookups/p", "reread", "inj KB", "ctx M/p", "cg")
    print("{:10} {:8} {:8} {:>7} {:>9} {:>6} {:>6} {:>7} {:>3}".format(*header))
    by_arm: dict[str, list] = collections.defaultdict(list)
    for sid, arm, date, s in rows:
        p = s["prompts"]
        print(
            f"{date:10} {sid[:8]:8} {arm:8} {p:7} {s['lookups'] / p:9.1f} {s['reread']:6} "
            f"{s['inj_bytes'] / 1024:6.0f} {s['ctx'] / p / 1e6:7.2f} {s['cg']:3}"
        )
        by_arm[arm].append(s)

    print("\nper arm (median of sessions)")
    for arm, ss in sorted(by_arm.items()):
        lookups = statistics.median(x["lookups"] / x["prompts"] for x in ss)
        ctx = statistics.median(x["ctx"] / x["prompts"] / 1e6 for x in ss)
        rereads = sum(x["reread"] for x in ss)
        print(
            f"  {arm:8} n={len(ss):<3} lookups/prompt={lookups:.1f} "
            f"ctx M/prompt={ctx:.2f} rereads={rereads}"
        )
    print("\nOnly sessions with an arm in .codegraph/ab-arms.log are a fair comparison.")


if __name__ == "__main__":
    main()
