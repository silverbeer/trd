"""The Telegram send guard (SB-1421).

What is worth pinning: every rule refuses, the refusal names the rule and never
the text it matched, the guard sits in front of the network on both send paths,
the messages trd actually sends still pass, and the guard's pattern lists are
the scanner's — the scanner is stdlib-only and cannot import them, so a test is
the only thing keeping the two copies one.
"""

import ast
import urllib.request
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.errors import NotifyError
from trd.models import AccountType, DailyBar, Side
from trd.models.alloc import CASH, AllocTarget, CashFlowKind, CashYield
from trd.notify import privacy
from trd.notify.bot import TelegramTransport
from trd.notify.telegram import TelegramNotifier

D = Decimal
SCANNER = Path(__file__).resolve().parents[1] / "scripts" / "privacy_scan.py"
# Built at runtime so this file holds no account-number shape for the tree scan.
ACCOUNT = "Z" + "41-" + "583920"


@pytest.fixture(autouse=True)
def no_private_terms(monkeypatch, tmp_path) -> None:
    """The owner's real list must never decide a test, either way."""
    monkeypatch.setenv("TRD_PRIVATE_TERMS", str(tmp_path / "absent.txt"))


def refused(text: str, **kw) -> str:
    with pytest.raises(NotifyError) as exc:
        privacy.guard(text, **kw)
    return str(exc.value)


# ------------------------------------------------------------------ the rules


@pytest.mark.parametrize(
    "text",
    [
        "approve BUY X in the Robinhood app",
        "you place it at Fidelity",
        "backtest fidelity",  # case-insensitive here: messages are prose
        "moved from SCHWAB",
        "an E*Trade transfer",
        "the etrade login",
        "Interactive  Brokers",
        "the sofi account",
        "via Public.com",
        "Fidel<b>ity</b>",  # a tag cannot split a name past the check
    ],
)
def test_a_brokerage_name_is_refused(text: str) -> None:
    assert "rule firm-name" in refused(text)


@pytest.mark.parametrize("ticker", privacy.MONEY_MARKET_TICKERS)
def test_a_core_money_market_fund_is_refused(ticker: str) -> None:
    assert "rule money-market" in refused(f"cash working in {ticker} at 3.5%")


def test_an_account_number_shape_is_refused() -> None:
    assert "rule account-number" in refused(f"held in {ACCOUNT}")
    assert "rule account-number" in refused("account no. " + "48" + "1937265")
    assert "rule account-number" in refused("4819" + "37265 is the acct")


def test_placeholders_and_ordinary_numbers_pass() -> None:
    for text in [
        "account TEST-ACCT-1",
        "account X00-000000",
        "account 00000000",
        "BUY SOFI 3 sh (~$45.00)",  # the ticker, a stock — not the firm
        "worth $100,240.00 — up $240.00",
        "20260903 scan, 1234567 bars",  # long numbers nowhere near 'account'
    ]:
        privacy.guard(text)


def test_a_private_term_is_refused(tmp_path) -> None:
    terms = tmp_path / "terms.txt"
    terms.write_text("# comments and blanks are ignored\n\nAcme Pension\n4242424\n")
    env = {"TRD_PRIVATE_TERMS": str(terms)}
    assert "rule private-term" in refused("rolled over from acme pension", env=env)
    assert "rule private-term" in refused("worth $4,242,424.00", env=env)
    assert "rule private-term" in refused("worth 4242424", env=env)
    privacy.guard("worth $14,242,424", env=env)  # a different number
    assert "rule private-term" in refused("Acme Pensioners", env=env)  # a term inside a word
    privacy.guard("Acme Pens", env=env)  # not the whole term


def test_the_refusal_never_carries_what_it_matched(tmp_path) -> None:
    terms = tmp_path / "terms.txt"
    terms.write_text("Zebulon\n")
    env = {"TRD_PRIVATE_TERMS": str(terms)}
    for text, value in [
        ("approve in the Robinhood app", "Robinhood"),
        ("cash in SPAXX", "SPAXX"),
        (f"held in {ACCOUNT}", ACCOUNT),
        ("from zebulon", "zebulon"),
    ]:
        message = refused(text, env=env)
        assert value.lower() not in message.lower(), message


# ------------------------------------------------------------------ both send paths


def test_the_notifier_refuses_before_anything_leaves(monkeypatch) -> None:
    def never(*_a, **_k):
        raise AssertionError("reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", never)
    notifier = TelegramNotifier("token", "chat")
    # A firm name is redacted, not refused (see redact_firms); what identifies
    # the account still stops the send before the network.
    with pytest.raises(NotifyError, match="rule money-market"):
        notifier.send("<b>SPAXX</b>", html=True)


def test_the_bot_refuses_before_anything_leaves(monkeypatch) -> None:
    def never(*_a, **_k):
        raise AssertionError("reached the network")

    monkeypatch.setattr(urllib.request, "urlopen", never)
    with pytest.raises(NotifyError, match="rule account-number"):
        TelegramTransport("token").send("chat", f"{ACCOUNT} holds it")


# ------------------------------------------------------------------ real messages pass


def test_the_ira_report_passes(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> None:
    from trd.notify.messages import alloc_report_message
    from trd.repos import AccountRepo
    from trd.services.alloc import AllocService, CashService
    from trd.services.alloc_report import AllocReportService
    from trd.services.portfolio import PortfolioService

    start, today = date(2026, 10, 26), date(2026, 10, 27)
    for symbol, price in [("IVV", "606"), ("IXUS", "80"), ("QQQM", "250"), ("SMH", "300")]:
        provider.add_symbol(symbol, price=price, prev_close=price)
    provider.add_symbol("SPY", price="101", prev_close="100")
    for symbol, then, now in [("IVV", "600", "606"), ("SPY", "100", "101")]:
        provider.add_bars(
            symbol,
            [
                DailyBar(date=on, open=D(c), high=D(c), low=D(c), close=D(c), adj_close=D(c))
                for on, c in [(start, then), (today, now)]
            ],
        )
    AccountRepo(conn).create("ira", AccountType.REAL)
    service = AllocService(conn, provider)
    targets = [
        AllocTarget(symbol="IVV", weight=D(45)),
        AllocTarget(symbol="IXUS", weight=D(20)),
        AllocTarget(symbol="QQQM", weight=D(25)),
        AllocTarget(symbol="SMH", weight=D(5)),
        AllocTarget(symbol=CASH, weight=D(5)),
    ]
    service.create("ira", "IRA Growth Plan", D("100000"), targets)
    CashService(conn).record("ira", CashFlowKind.DEPOSIT, D("100000"), datetime(2026, 10, 24))
    service.schedule("ira", start, D(60000), D(10000), 3, 4)
    # The stored fund is the brokerage's own core fund; the report must not say so.
    service.edit("ira", cash_yield=CashYield(fund="SPAXX", yield_pct=D("3.51"), as_of=today))
    PortfolioService(conn, provider).record_trade(
        account_name="ira",
        symbol="IVV",
        side=Side.BUY,
        quantity=D(40),
        price=D(600),
        executed_at=datetime(2026, 10, 26, 11),
    )
    text = alloc_report_message(AllocReportService(service).report("ira", today=today))
    assert "money market fund" in text and "you place it at your broker" in text
    privacy.guard(text)


def test_the_live_engine_messages_pass() -> None:
    from tests.test_engine_outbox import _row
    from trd.models import IntentStatus
    from trd.models.outbox import OutboxAck
    from trd.notify.messages import approval_message, intent_message
    from trd.notify.telegram import LIVE_LABEL

    row = _row()
    ack = OutboxAck(
        symbol="XYZ",
        from_status=IntentStatus.PENDING,
        to_status=IntentStatus.AWAITING_APPROVAL,
        changed=True,
        intent=row.intent,
    )
    for text in (intent_message(row, LIVE_LABEL), approval_message(ack, LIVE_LABEL)):
        assert "your broker app" in text
        privacy.guard(text)


def test_the_daily_report_passes() -> None:
    from tests.test_daily_report import TODAY, day
    from trd.notify.messages import daily_report_message
    from trd.services.daily_report import DailyReport

    text = daily_report_message(DailyReport(on=TODAY, engines=[day("swing"), day("day")]))
    privacy.guard(text)


# ------------------------------------------------------------------ one list, two copies


def scanner_constants() -> dict[str, object]:
    tree = ast.parse(SCANNER.read_text(encoding="utf-8"))
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    out[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    continue
    return out


@pytest.mark.parametrize(
    "name",
    [
        "FIRM_PATTERNS",
        "MONEY_MARKET_TICKERS",
        "ACCOUNT_NUMBER_PATTERNS",
        "PLACEHOLDERS",
        "PLACEHOLDER_PREFIXES",
        "DEFAULT_TERMS",
    ],
)
def test_the_guard_and_the_scanner_share_one_pattern_list(name: str) -> None:
    assert scanner_constants()[name] == getattr(privacy, name)


def test_a_firm_name_is_redacted_and_the_message_still_goes(monkeypatch) -> None:
    """A company name ("SoFi Technologies" in an /add reply, "SoFi" in the AI
    review) is not where money is held: blank it, keep the message."""
    from trd.notify.privacy import FIRM_REDACTION, redact_firms

    out = redact_firms("<b>review</b>: SoFi trades exited early · Robinhood Markets added")
    assert "SoFi" not in out and "Robinhood" not in out
    assert out.count(FIRM_REDACTION) == 2 and out.startswith("<b>review</b>")
    assert redact_firms("filled BUY SOFI 0.5") == "filled BUY SOFI 0.5"  # the ticker stays


def test_the_notifier_sends_a_redacted_firm_name(monkeypatch) -> None:
    import json as _json

    from trd.notify.telegram import TelegramNotifier

    monkeypatch.setenv("TRD_PRIVATE_TERMS", "/nonexistent")
    sent: list[dict] = []

    class _R:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout):
        sent.append(_json.loads(req.data))
        return _R()

    monkeypatch.setattr("trd.notify.telegram.urllib.request.urlopen", fake)
    TelegramNotifier("t", "1").send("review: Fidelity funds lagged")
    assert sent and "Fidelity" not in sent[0]["text"]
