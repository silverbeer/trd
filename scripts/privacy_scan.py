#!/usr/bin/env python3
"""Keep real account data out of a public repository (SB-1421).

This repository is public. trd tracks real retirement accounts, and the rule is
that no real account data ever enters the repo or its git metadata: no broker
export, no account number, no database, no backup, no real home path, no figure
the owner has listed as private, and no brokerage name in a commit message, PR
or branch name. See docs/privacy.md.

stdlib only and imports nothing from trd, so it runs in a bare git hook before
`uv sync` and in CI before anything is installed. The Telegram send guard
(src/trd/notify/privacy.py) keeps its own copy of the shared pattern lists;
tests/test_notify_privacy.py fails if the two drift.

Modes:
  --staged        ADDED lines of `git diff --cached`, plus every staged file's
                  name and content type (pre-commit)
  --message FILE  a commit message (commit-msg)
  --text          stdin: a PR title or body, a branch name (pre-push, CI)
  --tree          every tracked file (CI, audits)

A finding prints its rule id, where, and a REDACTED preview — first and last
character and the length, never the value. Exit 1 on any block, 0 otherwise
(warnings included).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# ---------------------------------------------------------------- shared lists
# Copied verbatim into src/trd/notify/privacy.py; a test compares the two.

# Brokerage firms, as regex fragments matched as whole tokens, case-insensitively.
FIRM_PATTERNS = (
    r"fidelity",
    r"robinhood",
    r"schwab",
    r"vanguard",
    r"e\s?[*-]?\s?trade",
    r"sofi",
    r"merrill",
    r"ameritrade",
    r"interactive\s+brokers",
    r"ibkr",
    r"webull",
    r"public\.com",
    r"wealthfront",
    r"betterment",
    r"firstrade",
    r"ally\s+invest",
)

# Core money-market ("sweep") funds. Each belongs to one brokerage, so naming
# one names where the cash is held.
MONEY_MARKET_TICKERS = (
    "SPAXX",
    "FDRXX",
    "FZFXX",
    "SPRXX",
    "FZDXX",
    "FCASH",
    "SWVXX",
    "VMFXX",
    "VMRXX",
)

# Account-number shapes. The named group `v` is the value that gets redacted.
ACCOUNT_NUMBER_PATTERNS = (
    r"(?<![A-Za-z0-9])(?P<v>[A-Z]\d{2}-\d{6})(?![0-9])",
    r"(?i:acct|account)[^\n\d]{0,20}(?<![0-9])(?P<v>\d{8,12})(?![0-9])",
    r"(?<![0-9])(?P<v>\d{8,12})(?![0-9])[^\n\d]{0,20}(?i:acct|account)",
)

# Values that are obviously placeholders, never real (exact, case-insensitive).
PLACEHOLDERS = ("USERNAME", "CHANGE_ME", "X00-000000")
PLACEHOLDER_PREFIXES = ("TEST-ACCT-",)

# ---------------------------------------------------------------- scanner-only

# Column signatures of broker export files, compared after the line is
# normalised (quotes and spaces removed, lowercased). The first two are the
# layouts trd's own importer reads (tests/fixtures/fidelity); the rest are the
# commonly published layouts of other brokers, from memory and best-effort:
# verify against a real export (locally, never committed) and extend.
EXPORT_HEADERS = (
    ("fidelity-activity", "rundate,action,symbol,description,type,price($),quantity"),
    ("fidelity-positions", "accountnumber,accountname,symbol,description,quantity,lastprice"),
    ("schwab-transactions", "date,action,symbol,description,quantity,price,fees&comm,amount"),
    ("vanguard-transactions", "accountnumber,tradedate,settlementdate,transactiontype"),
    ("robinhood-activity", "activitydate,processdate,settledate,instrument,description,transcode"),
    ("etrade-transactions", "transactiondate,transactiontype,securitytype,symbol,quantity"),
)

# Legal boilerplate a broker appends to a download.
EXPORT_FOOTERS = (
    "brokerage services are provided by",
    "date downloaded",
)

PERSONAL_PATH = r"/Users/(?P<v>[A-Za-z0-9._-]+)"
PATH_PLACEHOLDERS = ("USERNAME", "CHANGE_ME", "runner", "Shared")

FORBIDDEN_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".ofx",
    ".qfx",
    ".xlsx",
    ".xls",
    ".pem",
)
# `.env` files: allowed only when the name says it is a template.
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
# Top-level keys of `trd backup` (src/trd/services/backup.py export_data).
BACKUP_KEYS = frozenset(
    {"accounts", "transactions", "instruments", "cash_flows", "alloc_plans", "income"}
)

FIXTURES = "tests/fixtures/"
SYNTHETIC_LIST = "tests/fixtures/SYNTHETIC.md"
EXCEPTIONS_FILE = "scripts/privacy_exceptions.txt"
DEFAULT_TERMS = "~/.config/trd/private-terms.txt"

BLOCK, WARN = "BLOCK", "WARN"
# Past this many warnings, one summary line instead of one line each.
MAX_WARN_LINES = 10


@dataclass(frozen=True)
class Finding:
    level: str
    rule: str
    where: str
    preview: str

    def render(self) -> str:
        return f"{self.level} [{self.rule}] {self.where}: {self.preview}"


def redact(value: str) -> str:
    """First and last character and the length — enough to find it, not to read it."""
    value = value.strip()
    n = len(value)
    if n <= 4:
        return f"{'*' * n} ({n} chars)"
    return f"{value[0]}…{value[-1]} ({n} chars)"


def is_placeholder(value: str) -> bool:
    v = value.strip().upper()
    if v in PLACEHOLDERS or v.startswith(PLACEHOLDER_PREFIXES):
        return True
    digits = re.sub(r"\D", "", v)
    # 00000000, 99999999, 12345678: the numbers people type when they mean "a number".
    return bool(digits) and (len(set(digits)) == 1 or "1234567890123".startswith(digits))


def _token(fragment: str) -> str:
    return rf"(?<![A-Za-z0-9])(?:{fragment})(?![A-Za-z0-9])"


FIRM_RE = re.compile("|".join(f"({_token(f)})" for f in FIRM_PATTERNS), re.IGNORECASE)
MONEY_MARKET_RE = re.compile(_token("|".join(MONEY_MARKET_TICKERS)), re.IGNORECASE)
ACCOUNT_RES = tuple(re.compile(p) for p in ACCOUNT_NUMBER_PATTERNS)
PATH_RE = re.compile(PERSONAL_PATH)


def firm_blocks_in_message(match: str) -> bool:
    """In prose that is not code, lowercase `fidelity` is the ordinary word
    (`trd engine fidelity`, backtest fidelity); Fidelity/FIDELITY are the firm.
    SOFI in capitals is SoFi Technologies' ticker, a stock, not an account."""
    return match not in ("fidelity", "SOFI")


# ---------------------------------------------------------------- private terms


def term_pattern(term: str) -> re.Pattern[str]:
    """A whole-token, case-insensitive pattern. A number also matches with or
    without thousands separators: 1234567 finds 1,234,567 and 1_234_567."""
    plain = term.replace(",", "").replace("_", "")
    if re.fullmatch(r"\d+(\.\d+)?", plain):
        whole, _, frac = plain.partition(".")
        groups = []
        while whole:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        body = "[,_]?".join(groups) + (re.escape("." + frac) if frac else "")
        return re.compile(rf"(?<![\d,_.])(?P<v>{body})(?![\d]|[,_]\d)")
    return re.compile(rf"(?<![A-Za-z0-9])(?P<v>{re.escape(term)})(?![A-Za-z0-9])", re.IGNORECASE)


def load_private_terms(notice: list[str]) -> list[re.Pattern[str]]:
    raw = os.environ.get("TRD_PRIVATE_TERMS") or DEFAULT_TERMS
    path = Path(raw).expanduser()
    if not path.is_file():
        notice.append(f"note: no private terms file at {raw} — rule private-term skipped")
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            terms.append(term_pattern(line))
    return terms


# ---------------------------------------------------------------- line rules


def normalise(line: str) -> str:
    return re.sub(r"[\s\"']", "", line).lower()


def scan_line(
    line: str,
    where: str,
    terms: list[re.Pattern[str]],
    *,
    prose: bool,
    synthetic: bool = False,
) -> list[Finding]:
    """Every rule that reads a line of text. `prose` = a commit message, PR text
    or branch name, where firm names block; elsewhere (code) they only warn."""
    out: list[Finding] = []
    if not synthetic:
        flat = normalise(line)
        for name, signature in EXPORT_HEADERS:
            if signature in flat:
                out.append(Finding(BLOCK, "export-header", where, f"{name} column layout"))
        lowered = line.lower()
        for phrase in EXPORT_FOOTERS:
            if phrase in lowered:
                out.append(Finding(BLOCK, "export-footer", where, f"broker footer {phrase!r}"))
    for rx in ACCOUNT_RES:
        for m in rx.finditer(line):
            value = m.group("v")
            if not is_placeholder(value):
                out.append(Finding(BLOCK, "account-number", where, redact(value)))
    for m in PATH_RE.finditer(line):
        name = m.group("v")
        if name not in PATH_PLACEHOLDERS:
            out.append(Finding(BLOCK, "home-path", where, f"/Users/{redact(name)}"))
    for rx in terms:
        for m in rx.finditer(line):
            out.append(Finding(BLOCK, "private-term", where, redact(m.group("v"))))
    for m in FIRM_RE.finditer(line):
        if prose and firm_blocks_in_message(m.group(0)):
            out.append(Finding(BLOCK, "firm-name", where, redact(m.group(0))))
        elif not prose:
            out.append(Finding(WARN, "firm-name", where, redact(m.group(0))))
    if prose:
        for m in MONEY_MARKET_RE.finditer(line):
            out.append(Finding(BLOCK, "money-market", where, redact(m.group(0))))
    return out


# ---------------------------------------------------------------- file rules


def file_name_findings(path: str, synthetic: set[str]) -> list[Finding]:
    p = PurePosixPath(path)
    name = p.name.lower()
    where = path
    if ".duckdb" in name:
        return [Finding(BLOCK, "forbidden-file", where, "DuckDB database")]
    if name.endswith(FORBIDDEN_SUFFIXES):
        return [Finding(BLOCK, "forbidden-file", where, f"{p.suffix} file")]
    if name.startswith(".env") and not name.endswith(ENV_TEMPLATE_SUFFIXES):
        return [Finding(BLOCK, "forbidden-file", where, "environment file")]
    if name.endswith(".csv") and not (path.startswith(FIXTURES) and path in synthetic):
        return [Finding(BLOCK, "forbidden-file", where, "CSV outside the synthetic fixtures")]
    return []


def content_findings(path: str, data: bytes) -> list[Finding]:
    """What a file IS, whatever it is called."""
    if data.startswith(b"SQLite format 3\x00"):
        return [Finding(BLOCK, "forbidden-file", path, "SQLite database (by content)")]
    if data[8:12] == b"DUCK":
        return [Finding(BLOCK, "forbidden-file", path, "DuckDB database (by content)")]
    head = data.lstrip()[:1]
    if head == b"{" and len(data) < 200_000_000:
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return []
        if isinstance(doc, dict) and "version" in doc and len(BACKUP_KEYS & doc.keys()) >= 2:
            return [Finding(BLOCK, "backup-json", path, "shape of a `trd backup` file")]
    return []


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def scan_file_text(
    path: str, text: str, terms: list[re.Pattern[str]], synthetic: set[str]
) -> list[Finding]:
    exempt = path.startswith(FIXTURES) and path in synthetic
    out: list[Finding] = []
    for n, line in enumerate(text.splitlines(), 1):
        out += scan_line(line, f"{path}:{n}", terms, prose=False, synthetic=exempt)
    return out


# ---------------------------------------------------------------- git helpers


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def synthetic_list(read) -> set[str]:
    """Paths named in tests/fixtures/SYNTHETIC.md, each in backticks."""
    try:
        text = read(SYNTHETIC_LIST)
    except (subprocess.CalledProcessError, OSError):
        return set()
    return set(re.findall(r"`(tests/fixtures/[^`]+)`", text.decode("utf-8", "replace")))


def load_exceptions(read) -> list[tuple[str, int | None, str]]:
    """`path[:line] rule reason...` per line; `#` comments."""
    try:
        text = read(EXCEPTIONS_FILE).decode("utf-8")
    except (subprocess.CalledProcessError, OSError):
        return []
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            raise SystemExit(f"{EXCEPTIONS_FILE}: needs 'path[:line] rule reason': {raw!r}")
        loc, rule = parts[0], parts[1]
        path, _, num = loc.partition(":")
        out.append((path, int(num) if num else None, rule))
    return out


def excepted(f: Finding, exceptions: list[tuple[str, int | None, str]]) -> bool:
    path, _, num = f.where.partition(":")
    line = int(num) if num.isdigit() else None
    return any(p == path and r == f.rule and (n is None or n == line) for p, n, r in exceptions)


# ---------------------------------------------------------------- modes


def mode_staged(terms: list[re.Pattern[str]]) -> list[Finding]:
    def read_index(path: str) -> bytes:
        return git("show", f":{path}")

    synthetic = synthetic_list(read_index)
    out: list[Finding] = []
    names = git("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR").split(b"\0")
    for raw in filter(None, names):
        path = raw.decode("utf-8", "replace")
        out += file_name_findings(path, synthetic)
        with contextlib.suppress(subprocess.CalledProcessError):  # a submodule: nothing to read
            out += content_findings(path, read_index(path))
    diff = git("diff", "--cached", "--no-color", "--no-ext-diff", "-U0", "--diff-filter=ACMR")
    path, line_no = "", 0
    for raw in diff.decode("utf-8", "replace").splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else target
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+"):
            exempt = path.startswith(FIXTURES) and path in synthetic
            out += scan_line(raw[1:], f"{path}:{line_no}", terms, prose=False, synthetic=exempt)
            line_no += 1
    return out


def mode_tree(terms: list[re.Pattern[str]]) -> list[Finding]:
    def read_disk(path: str) -> bytes:
        return Path(path).read_bytes()

    synthetic = synthetic_list(read_disk)
    out: list[Finding] = []
    for raw in filter(None, git("ls-files", "-z").split(b"\0")):
        path = raw.decode("utf-8", "replace")
        out += file_name_findings(path, synthetic)
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue  # deleted in the work tree
        out += content_findings(path, data)
        if not is_binary(data):
            out += scan_file_text(path, data.decode("utf-8", "replace"), terms, synthetic)
    return out


def mode_prose(text: str, label: str, terms: list[re.Pattern[str]]) -> list[Finding]:
    out: list[Finding] = []
    for n, line in enumerate(text.splitlines(), 1):
        out += scan_line(line, f"{label}:{n}", terms, prose=True)
    return out


def commit_message_text(path: str) -> str:
    """The message as git will store it: comment lines and the verbose-mode diff
    below the scissors line are not part of it."""
    lines = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# ------------------------ >8 ------------------------"):
            break
        if not line.startswith("#"):
            lines.append(line)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--message", metavar="FILE")
    mode.add_argument("--text", action="store_true")
    mode.add_argument("--tree", action="store_true")
    ap.add_argument("--label", default="text", help="what --text is, for the report")
    args = ap.parse_args(argv)

    notices: list[str] = []
    terms = load_private_terms(notices)
    if args.staged:
        findings = mode_staged(terms)
        exceptions = load_exceptions(lambda p: git("show", f":{p}"))
    elif args.tree:
        findings = mode_tree(terms)
        exceptions = load_exceptions(lambda p: Path(p).read_bytes())
    elif args.message:
        findings = mode_prose(commit_message_text(args.message), "commit message", terms)
        exceptions = []
    else:
        findings = mode_prose(sys.stdin.read(), args.label, terms)
        exceptions = []

    findings = [f for f in findings if not excepted(f, exceptions)]
    for note in notices:
        print(f"privacy-scan: {note}", file=sys.stderr)

    blocks = [f for f in findings if f.level == BLOCK]
    warns = [f for f in findings if f.level == WARN]
    if len(warns) > MAX_WARN_LINES:
        # Firm names in code are a pending owner decision; a per-line list of
        # several hundred would bury the blocks. Count them per file instead.
        per_file: dict[str, int] = {}
        for f in warns:
            path = f.where.partition(":")[0]
            per_file[path] = per_file.get(path, 0) + 1
        print(
            f"privacy-scan: WARN [firm-name] {len(warns)} mention(s) in {len(per_file)} "
            "file(s) — allowed in code, see docs/privacy.md",
            file=sys.stderr,
        )
    else:
        for f in warns:
            print(f"privacy-scan: {f.render()}", file=sys.stderr)
    for f in blocks:
        print(f"privacy-scan: {f.render()}", file=sys.stderr)
    if blocks:
        print(
            f"privacy-scan: {len(blocks)} blocking finding(s). This repository is public: "
            "see docs/privacy.md. Values are redacted above on purpose.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
