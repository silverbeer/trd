#!/usr/bin/env python3
"""Keep real account data out of a public repository (SB-1421, SB-1429).

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

Text is matched after FOLDING (SB-1429): NFKC-normalised, zero-width and other
format characters removed, lowercased. A private term of six characters or more
matches inside a longer token and with any of space, `-`, `_` or `.` between its
words, so `jane-example`, `jane_example` and `janeexample` all find `Jane
Example`; a shorter term still has to stand alone. Two consecutive lines are
also read as one, so a term split across a line break is found. File names,
and the printable strings inside a binary file, are scanned like text.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections.abc import Collection, Sequence
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
# A grouped match (digits split by spaces or dashes) is kept only when it holds
# 8-12 digits and is not a date — see `is_placeholder`.
ACCOUNT_NUMBER_PATTERNS = (
    r"(?<![A-Za-z0-9])(?P<v>[A-Z]\d{2}[ -]\d{6})(?![0-9])",
    r"(?i:acct|account)[^\n\d]{0,20}(?<![0-9])(?P<v>\d{8,12})(?![0-9])",
    r"(?<![0-9])(?P<v>\d{8,12})(?![0-9])[^\n\d]{0,20}(?i:acct|account)",
    r"(?i:acct|account)[^\n\d]{0,20}(?<![0-9])(?P<v>\d{2,4}(?:[ -]\d{2,4}){1,3})(?![0-9])",
    r"(?<![0-9])(?P<v>\d{2,4}(?:[ -]\d{2,4}){1,3})(?![0-9])[^\n\d]{0,20}(?i:acct|account)",
    r"(?<![A-Za-z0-9*.])(?P<v>(?:\*{4,}|[xX]{4,}|\.{3})\d{4})(?![0-9])",
    r"(?i:acct|account)[^\n\d]{0,20}(?<![A-Za-z0-9])(?P<v>X\d{4,5})(?![0-9])",
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

# A data ROW of an activity export, for a file whose header and footer were cut
# off: one of the broker's action phrases, a US date, and at least six commas on
# the same line. Code and docs that explain the importer name the phrases but
# never in that shape.
EXPORT_ROW_ACTIONS = (
    "YOU BOUGHT",
    "YOU SOLD",
    "DIVIDEND RECEIVED",
    "REINVESTMENT",
    "ADVISORY FEE",
    "ROLLOVER",
    "CONTRIBUTION",
    "JOURNALED",
    "TRANSFERRED",
    "ELECTRONIC FUNDS TRANSFER",
)
EXPORT_ROW_MIN_COMMAS = 6
US_DATE_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")

# `/Users/<name>` (macOS), `/home/<name>` (Linux) and `~<name>/` all name a person.
# `~` alone, or as "approximately", is not a path.
PERSONAL_PATH = r"(?:/Users|/home)/(?P<v>[A-Za-z0-9._-]+)"
TILDE_PATH = r"(?<![A-Za-z0-9@/])~(?P<v>[A-Za-z][A-Za-z0-9._-]{1,31})(?=/)"
PATH_PLACEHOLDERS = ("USERNAME", "CHANGE_ME", "runner", "Shared", "user", "ubuntu", "USER")

FORBIDDEN_SUFFIXES = (
    ".db",
    ".sqlite",
    ".sqlite3",
    ".ofx",
    ".qfx",
    ".qif",
    ".xlsx",
    ".xls",
    ".xlsm",
    ".numbers",
    ".ods",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".ppk",
    ".jsonl",
    ".parquet",
    ".feather",
    ".pkl",
    ".pickle",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tgz",
    ".gz",
    ".bz2",
    ".xz",
)
# Tabular text: allowed only as a listed synthetic fixture.
TABULAR_SUFFIXES = (".csv", ".tsv")
# `.env` files: allowed only when the name says it is a template.
ENV_TEMPLATE_SUFFIXES = (".example", ".sample", ".template")
# Top-level keys of `trd backup` (src/trd/services/backup.py export_data). One
# of them holding a list is enough: a backup with a single table is still a
# backup, and nothing else in this tree uses these names.
BACKUP_KEYS = frozenset(
    {
        "accounts",
        "transactions",
        "instruments",
        "cash_flows",
        "alloc_plans",
        "income",
        "plans",
        "watchlists",
        "earnings_results",
    }
)
# A binary file is allowed only as one of these asset types; its printable
# strings are scanned either way.
BINARY_ALLOWED_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".pdf",
)
# Plain-text files that a spreadsheet or export is pasted into under a harmless
# name. Two or more consecutive lines with the same count of 4+ commas or tabs
# is a table, not prose.
DELIMITED_SUFFIXES = (".txt", ".dat", ".log", ".tsv", "")
DELIMITED_MIN_FIELDS = 5
DELIMITED_MIN_ROWS = 2

# A private term this long or longer is also found INSIDE a longer token
# (`maplewoodira` for `maplewood`); a shorter one must stand alone, or `main`
# would hit `domain`.
SUBSTRING_MIN_LEN = 6
# What may sit between the words of a multi-word term: a slug, a snake, nothing.
WORD_JOINER = r"[\s_\-.]*"

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


def is_date(value: str) -> bool:
    """2026-10-25, 10-25-2026, 25 10 2026: grouped digits that are a date, not an account."""
    v = value.strip()
    return bool(
        re.fullmatch(r"\d{4}[ -]\d{1,2}[ -]\d{1,2}", v)
        or re.fullmatch(r"\d{1,2}[ -]\d{1,2}[ -]\d{4}", v)
    )


def is_placeholder(value: str) -> bool:
    v = value.strip().upper()
    if v in PLACEHOLDERS or v.startswith(PLACEHOLDER_PREFIXES):
        return True
    if is_date(v):
        return True
    digits = re.sub(r"\D", "", v)
    if not digits:
        return False
    if (" " in v or "-" in v) and not 8 <= len(digits) <= 12:
        return True  # a grouped number is an account only at account length
    # 00000000, 99999999, 12345678: the numbers people type when they mean "a number".
    return len(set(digits)) == 1 or "1234567890123".startswith(digits)


def _token(fragment: str) -> str:
    return rf"(?<![A-Za-z0-9])(?:{fragment})(?![A-Za-z0-9])"


FIRM_RE = re.compile("|".join(f"({_token(f)})" for f in FIRM_PATTERNS), re.IGNORECASE)
MONEY_MARKET_RE = re.compile(_token("|".join(MONEY_MARKET_TICKERS)), re.IGNORECASE)
ACCOUNT_RES = tuple(re.compile(p) for p in ACCOUNT_NUMBER_PATTERNS)
PATH_RE = re.compile(PERSONAL_PATH)
TILDE_RE = re.compile(TILDE_PATH)


def firm_blocks_in_message(match: str) -> bool:
    """In prose that is not code, lowercase `fidelity` is the ordinary word
    (`trd engine fidelity`, backtest fidelity); Fidelity/FIDELITY are the firm.
    SOFI in capitals is SoFi Technologies' ticker, a stock, not an account."""
    return match not in ("fidelity", "SOFI")


# ---------------------------------------------------------------- private terms


def fold(text: str) -> str:
    """The text as a reader sees it: compatibility-normalised, format characters
    (zero-width space and joiner, soft hyphen, direction marks) dropped, lowercased."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf").lower()


def term_pattern(term: str) -> re.Pattern[str]:
    """A case-insensitive pattern for a FOLDED line. A number also matches with
    or without thousands separators: 1234567 finds 1,234,567 and 1_234_567. A
    word term matches with any joiner between its words; at six characters or
    more it also matches inside a longer token."""
    plain = term.replace(",", "").replace("_", "")
    if re.fullmatch(r"\d+(\.\d+)?", plain):
        whole, _, frac = plain.partition(".")
        groups = []
        while whole:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        body = "[,_]?".join(groups) + (re.escape("." + frac) if frac else "")
        return re.compile(rf"(?<![\d,_.])(?P<v>{body})(?![\d]|[,_]\d)")
    words = [w for w in re.split(r"[\s_\-.]+", fold(term)) if w]
    body = WORD_JOINER.join(re.escape(w) for w in words)
    if len("".join(words)) >= SUBSTRING_MIN_LEN:
        return re.compile(rf"(?P<v>{body})")
    return re.compile(rf"(?<![a-z0-9])(?P<v>{body})(?![a-z0-9])")


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


def term_findings(text: str, where: str, terms: Sequence[re.Pattern[str]]) -> list[Finding]:
    folded = fold(text)
    return [
        Finding(BLOCK, "private-term", where, redact(m.group("v")))
        for rx in terms
        for m in rx.finditer(folded)
    ]


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
        if line.count(",") >= EXPORT_ROW_MIN_COMMAS and US_DATE_RE.search(line):
            for action in EXPORT_ROW_ACTIONS:
                if action in line:
                    out.append(Finding(BLOCK, "export-row", where, f"activity row {action!r}"))
                    break
    for rx in ACCOUNT_RES:
        for m in rx.finditer(line):
            value = m.group("v")
            if not is_placeholder(value):
                out.append(Finding(BLOCK, "account-number", where, redact(value)))
    for m in PATH_RE.finditer(line):
        name = m.group("v")
        if name not in PATH_PLACEHOLDERS:
            out.append(Finding(BLOCK, "home-path", where, f"home of {redact(name)}"))
    for m in TILDE_RE.finditer(line):
        name = m.group("v")
        if name not in PATH_PLACEHOLDERS:
            out.append(Finding(BLOCK, "home-path", where, f"~ of {redact(name)}"))
    out += term_findings(line, where, terms)
    for m in FIRM_RE.finditer(line):
        if prose and firm_blocks_in_message(m.group(0)):
            out.append(Finding(BLOCK, "firm-name", where, redact(m.group(0))))
        elif not prose:
            out.append(Finding(WARN, "firm-name", where, redact(m.group(0))))
    if prose:
        for m in MONEY_MARKET_RE.finditer(line):
            out.append(Finding(BLOCK, "money-market", where, redact(m.group(0))))
    return out


def scan_lines(
    lines: list[str],
    where: str,
    terms: list[re.Pattern[str]],
    *,
    prose: bool,
    synthetic: bool = False,
    first_line: int = 1,
) -> list[Finding]:
    """Every line on its own, then every pair of neighbours joined (with and
    without a space, and with quotes, brackets and `+` dropped, which is how a
    string is continued in code) so a term split across a line break is still
    one term.
    A pair finding is reported at the first line, once per term, and only when
    neither line alone matched that term."""
    out: list[Finding] = []
    prev: str | None = None
    for i, line in enumerate(lines):
        n = first_line + i
        out += scan_line(line, f"{where}:{n}", terms, prose=prose, synthetic=synthetic)
        if prev is not None and terms:
            folded_prev, folded_line = fold(prev), fold(line)
            for rx in terms:
                if rx.search(folded_prev) or rx.search(folded_line):
                    continue
                glued = re.sub(r"[\s\"'+()]+", "", folded_prev + folded_line)
                for joined in (folded_prev + folded_line, folded_prev + " " + folded_line, glued):
                    m = rx.search(joined)
                    if m:
                        preview = f"{redact(m.group('v'))}, split across lines"
                        out.append(Finding(BLOCK, "private-term", f"{where}:{n - 1}", preview))
                        break
        prev = line
    return out


# ---------------------------------------------------------------- file rules


def file_name_findings(
    path: str, synthetic: Collection[str], terms: Sequence[re.Pattern[str]] = ()
) -> list[Finding]:
    p = PurePosixPath(path)
    name = p.name.lower()
    where = path
    out: list[Finding] = []
    if ".duckdb" in name:
        out.append(Finding(BLOCK, "forbidden-file", where, "DuckDB database"))
    elif name.endswith(FORBIDDEN_SUFFIXES):
        out.append(Finding(BLOCK, "forbidden-file", where, f"{p.suffix} file"))
    elif (name.startswith(".env") or name.endswith((".env", ".envrc"))) and not name.endswith(
        ENV_TEMPLATE_SUFFIXES
    ):
        out.append(Finding(BLOCK, "forbidden-file", where, "environment file"))
    elif name.endswith(TABULAR_SUFFIXES) and not (path.startswith(FIXTURES) and path in synthetic):
        out.append(
            Finding(BLOCK, "forbidden-file", where, f"{p.suffix} outside the synthetic fixtures")
        )
    for f in term_findings(path, where, terms):
        out.append(Finding(BLOCK, "private-term", where, f"{f.preview}, in the file name"))
    for m in FIRM_RE.finditer(path):
        out.append(Finding(WARN, "firm-name", where, f"{redact(m.group(0))}, in the file name"))
    return out


def _delimited_rows(text: str) -> int:
    """Longest run of consecutive lines sharing the same count (>= 4) of commas or tabs."""
    best = run = 0
    last: tuple[str, int] | None = None
    for line in text.splitlines():
        key = None
        for sep in (",", "\t"):
            count = line.count(sep)
            if count >= DELIMITED_MIN_FIELDS - 1:
                key = (sep, count)
                break
        if key is not None and key == last:
            run += 1
        else:
            run = 1 if key is not None else 0
        last = key
        best = max(best, run)
    return best


def content_findings(
    path: str, data: bytes, synthetic: Collection[str] = frozenset()
) -> list[Finding]:
    """What a file IS, whatever it is called."""
    if data.startswith(b"SQLite format 3\x00"):
        return [Finding(BLOCK, "forbidden-file", path, "SQLite database (by content)")]
    if data[8:12] == b"DUCK":
        return [Finding(BLOCK, "forbidden-file", path, "DuckDB database (by content)")]
    if data.startswith(b"PK\x03\x04"):
        return [
            Finding(
                BLOCK, "forbidden-file", path, "zip archive or Office/Numbers document (by content)"
            )
        ]
    head = data[:512]
    if (b"-----BEGIN " in head and b"PRIVATE KEY" in head) or head.startswith(
        b"PuTTY-User-Key-File"
    ):
        return [Finding(BLOCK, "forbidden-file", path, "private key (by content)")]
    first = data.lstrip()[:1]
    if first == b"{" and len(data) < 200_000_000:
        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return []
        if isinstance(doc, dict) and any(isinstance(doc.get(k), list) for k in BACKUP_KEYS):
            return [Finding(BLOCK, "backup-json", path, "shape of a `trd backup` file")]
        return []
    tabular = PurePosixPath(path).suffix.lower() in DELIMITED_SUFFIXES and not is_binary(data)
    if tabular and not (path.startswith(FIXTURES) and path in synthetic):
        rows = _delimited_rows(data.decode("utf-8", "replace"))
        if rows >= DELIMITED_MIN_ROWS:
            return [
                Finding(BLOCK, "delimited-text", path, f"{rows} rows of comma/tab separated data")
            ]
    return []


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def binary_findings(path: str, data: bytes, terms: list[re.Pattern[str]]) -> list[Finding]:
    """A binary file is allowed only as a known asset type, and its printable
    strings are read like text either way — an xlsx renamed .png is still a
    spreadsheet full of names."""
    out: list[Finding] = []
    if not path.lower().endswith(BINARY_ALLOWED_SUFFIXES):
        out.append(
            Finding(BLOCK, "forbidden-file", path, "binary file that is not an allowed asset type")
        )
    strings = [s.decode("ascii") for s in re.findall(rb"[\x20-\x7e]{6,}", data)]
    out += scan_lines(strings, f"{path}:binary", terms, prose=False, first_line=0)
    return out


def scan_file_text(
    path: str, text: str, terms: list[re.Pattern[str]], synthetic: set[str]
) -> list[Finding]:
    exempt = path.startswith(FIXTURES) and path in synthetic
    return scan_lines(text.splitlines(), path, terms, prose=False, synthetic=exempt)


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
    binary: set[str] = set()
    for raw in filter(None, names):
        path = raw.decode("utf-8", "replace")
        out += file_name_findings(path, synthetic, terms)
        with contextlib.suppress(subprocess.CalledProcessError):  # a submodule: nothing to read
            data = read_index(path)
            out += content_findings(path, data, synthetic)
            if is_binary(data):
                binary.add(path)
                out += binary_findings(path, data, terms)
    diff = git("diff", "--cached", "--no-color", "--no-ext-diff", "-U0", "--diff-filter=ACMR")
    path, line_no = "", 0
    run: list[str] = []
    run_start = 0

    def flush() -> None:
        nonlocal run
        if run and path not in binary:
            exempt = path.startswith(FIXTURES) and path in synthetic
            out.extend(
                scan_lines(run, path, terms, prose=False, synthetic=exempt, first_line=run_start)
            )
        run = []

    for raw in diff.decode("utf-8", "replace").splitlines():
        if raw.startswith("+++ "):
            flush()
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else target
            continue
        if raw.startswith("@@"):
            flush()
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if raw.startswith("+"):
            if not run:
                run_start = line_no
            run.append(raw[1:])
            line_no += 1
        else:
            flush()
    flush()
    return out


def mode_tree(terms: list[re.Pattern[str]]) -> list[Finding]:
    def read_disk(path: str) -> bytes:
        return Path(path).read_bytes()

    synthetic = synthetic_list(read_disk)
    out: list[Finding] = []
    for raw in filter(None, git("ls-files", "-z").split(b"\0")):
        path = raw.decode("utf-8", "replace")
        out += file_name_findings(path, synthetic, terms)
        try:
            data = Path(path).read_bytes()
        except OSError:
            continue  # deleted in the work tree
        out += content_findings(path, data, synthetic)
        if is_binary(data):
            out += binary_findings(path, data, terms)
        else:
            out += scan_file_text(path, data.decode("utf-8", "replace"), terms, synthetic)
    return out


def mode_prose(text: str, label: str, terms: list[re.Pattern[str]]) -> list[Finding]:
    return scan_lines(text.splitlines(), label, terms, prose=True)


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

    findings = [f for f in dict.fromkeys(findings) if not excepted(f, exceptions)]
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
