"""The last check before a message leaves for Telegram (SB-1421).

Telegram is a third party and a channel can be read by anyone who has the link,
so a message never says where money is held: no brokerage name, no core
money-market fund (each belongs to one brokerage), no account number, and no
term from the owner's private list. A message that would is refused whole —
fail closed — rather than edited: a guard that rewrites text hides the renderer
bug that produced it.

The refusal names the rule, never the matched text; an error message ends up in
a log, and a log is exactly where the value must not land either.

The pattern lists are copied from scripts/privacy_scan.py, which is stdlib-only
and imports nothing from trd so it can run in a bare git hook. A test reads the
scanner as text and fails when the two copies drift.
"""

import html
import os
import re
import unicodedata
from pathlib import Path

from trd.errors import NotifyError

# ---------------------------------------------------------------- shared lists
# Identical to scripts/privacy_scan.py; tests/test_notify_privacy.py compares them.

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

# ---------------------------------------------------------------- guard-only

DEFAULT_TERMS = "~/.config/trd/private-terms.txt"
# SoFi Technologies' ticker: a stock the engines may trade, not where money is held.
TICKER_NOT_FIRM = "SOFI"


def _token(fragment: str) -> str:
    return rf"(?<![A-Za-z0-9])(?:{fragment})(?![A-Za-z0-9])"


_FIRM = re.compile("|".join(_token(f) for f in FIRM_PATTERNS), re.IGNORECASE)
_MONEY_MARKET = re.compile(_token("|".join(MONEY_MARKET_TICKERS)), re.IGNORECASE)
_ACCOUNT = tuple(re.compile(p) for p in ACCOUNT_NUMBER_PATTERNS)


def is_date(value: str) -> bool:
    """2026-10-25, 10-25-2026: grouped digits that are a date, not an account."""
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
    return len(set(digits)) == 1 or "1234567890123".startswith(digits)


SUBSTRING_MIN_LEN = 6
WORD_JOINER = r"[\s_\-.]*"


def fold(text: str) -> str:
    """The text as a reader sees it: NFKC-normalised, format characters (zero-width
    space and joiner, soft hyphen) dropped, lowercased. Same as the scanner's."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf").lower()


def term_pattern(term: str) -> re.Pattern[str]:
    """A pattern for a FOLDED text; a number also matches with or without
    thousands separators, a word term with any joiner between its words and,
    at six characters or more, inside a longer token. Same construction as the
    scanner's."""
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


def private_terms(env: dict[str, str] | None = None) -> list[re.Pattern[str]]:
    """The owner's private list, if this machine has one. In a pod it does not,
    and the other rules still apply."""
    source = env if env is not None else dict(os.environ)
    path = Path(source.get("TRD_PRIVATE_TERMS") or DEFAULT_TERMS).expanduser()
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(term_pattern(line))
    return out


def violation(text: str, terms: list[re.Pattern[str]] | None = None) -> str | None:
    """The id of the first rule `text` breaks, or None. HTML is read both as
    sent and as displayed, so a tag cannot split a name past the check."""
    shown = html.unescape(re.sub(r"<[^>]+>", "", text))
    for body in (text, shown):
        if any(m.group(0) != TICKER_NOT_FIRM for m in _FIRM.finditer(body)):
            return "firm-name"
        if _MONEY_MARKET.search(body):
            return "money-market"
        for rx in _ACCOUNT:
            if any(not is_placeholder(m.group("v")) for m in rx.finditer(body)):
                return "account-number"
        folded = fold(body)
        for rx in terms or []:
            if rx.search(folded):
                return "private-term"
    return None


FIRM_REDACTION = "[firm]"


def redact_firms(text: str) -> str:
    """Brokerage names blanked out, everything else untouched.

    A firm name is usually a COMPANY being talked about, not where the money
    sits: the bot's /add reply names "SoFi Technologies", and the nightly AI
    review can say "SoFi trades exited early". Refusing those would silently
    drop a whole review over a stock's name, so the name is redacted and the
    message still goes. What CAN identify the account — an account number, a
    private term, a custodian's money-market ticker — still refuses the send.
    Only applied to plain text spans, so HTML tags are never cut in half.
    """
    parts = re.split(r"(<[^>]+>)", text)
    return "".join(
        part
        if part.startswith("<")
        else _FIRM.sub(
            lambda m: m.group(0) if m.group(0) == TICKER_NOT_FIRM else FIRM_REDACTION, part
        )
        for part in parts
    )


def guard(text: str, env: dict[str, str] | None = None) -> None:
    """Raise NotifyError naming the rule — never the text — when `text` would
    say where money is held."""
    rule = violation(text, private_terms(env))
    if rule is not None:
        raise NotifyError(
            f"Telegram message NOT sent: the privacy guard refused it (rule {rule}). "
            "A message must never say where money is held — see docs/privacy.md."
        )
