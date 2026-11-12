"""SB-1628: a free slot that buying power cannot fill is stored, shown, alerted.

Before this, an entry refused for cash left one line in the scan's `skipped` —
a pod log — and an empty slot under a green watchlist read as "nothing to buy".
"""

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from trd.engine.settlement import CashLedger
from trd.models import CashBlock, CashRefusal, EngineMode
from trd.notify.messages import cash_alert_message, live_report_message
from trd.services.daily_report import EngineDay
from trd.services.engine import EngineService, ScanResult
from trd.services.live_report import LiveReport

from .conftest import FakeProvider
from .test_engine_guards import _buys
from .test_engine_live import MONDAY, _live, _names
from .test_engine_outbox import _no_telegram  # noqa: F401  (autouse)

D = Decimal
SESSION = date(2024, 9, 16)


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _ledger(settled: str, unsettled: str = "0", bridge: str = "0") -> CashLedger:
    pending = [(date(2024, 9, 17), D(unsettled))] if D(unsettled) else []
    return CashLedger(
        capital=D("100"),
        settled=D(settled),
        unsettled=D(unsettled),
        pending=pending,
        bridge=D(bridge),
    )


# ---------------------------------------------------------------- the ledger


def test_no_block_on_a_full_book_or_when_one_entry_is_covered():
    poor = _ledger("5")
    assert poor.block(free_slots=0, entry_cost=D("20")) is None  # full book needs no cash
    assert _ledger("20").block(free_slots=1, entry_cost=D("20")) is None


def test_a_fraction_of_a_cent_short_is_still_short_and_says_so():
    """The live case: $19.9993 printed as $20.00 while every $20 entry was refused."""
    cash = _ledger("-20.6383543962", unsettled="40.6376662925", bridge="57")
    block = cash.block(free_slots=1, entry_cost=D("20"))
    assert block is not None
    assert block.short == D("0.01")  # rounded UP, never to $0.00
    assert block.line() == (
        "cash-blocked: 1 free slot, $0.01 short of one $20.00 entry — $40.64 settles 2024-09-17"
    )


def test_block_with_nothing_settling_says_only_capital_refills_it():
    block = _ledger("3").block(free_slots=2, entry_cost=D("20"))
    assert block is not None
    assert block.line().endswith(
        "2 free slots, $17.00 short of one $20.00 entry — nothing pending settlement"
    )


# ---------------------------------------------------------------- the scan


def test_a_cash_refusal_is_stored_and_alerts_once_a_session(engine, provider, conn):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], capital=D("4999.99"))  # one cent under a $5,000 entry

    first = engine.scan(at=MONDAY)
    assert _buys(first) == []
    assert first.cash_refused, first.skipped
    assert {r.symbol for r in first.cash_refused} <= {"AAA", "BBB"}
    assert first.cash_refused[0].short == D("0.01")
    assert first.cash_alert  # the session's first refusal

    second = engine.scan(at=MONDAY + timedelta(minutes=5))
    assert second.cash_refused
    assert not second.cash_alert  # same news, same session

    account = engine.account()
    stored = engine.cash_refusals.list_on(account.id, SESSION)
    # One row per name and strategy, however many passes refused it.
    assert len(stored) == len({(r.symbol, r.strategy) for r in first.cash_refused})


def test_a_signals_only_pass_stores_but_never_alerts(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"], capital=D("100"))
    result = engine.scan(at=MONDAY, paper=False)
    assert not result.cash_alert


def test_status_reports_the_block(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"], capital=D("4999.99"))
    status = engine.status()
    assert status.cash_block is not None
    assert status.cash_block.free_slots == 5
    assert status.cash_block.short == D("0.01")
    assert status.cash_next_amount is None


def test_a_well_funded_engine_has_no_block(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    assert engine.status().cash_block is None


# ---------------------------------------------------------------- messages


def _refusal(symbol: str, score: float) -> CashRefusal:
    return CashRefusal(
        symbol=symbol,
        strategy="pullback",
        score=score,
        cost=D("19.99"),
        buying_power=D("17.0"),
        settles_on=date(2024, 9, 17),
        settles_amount=D("40.64"),
    )


def test_cash_alert_names_the_shortfall_the_signals_and_when_it_lifts():
    result = ScanResult(
        run_id=1,
        at=MONDAY,
        paper=True,
        scanned=10,
        mode=EngineMode.LIVE,
        cash_refused=[_refusal("PANW", 0.74), _refusal("SNOW", 0.73)],
        cash_alert=True,
    )
    text = cash_alert_message(result)
    assert text is not None
    assert "slot free, cash short" in text
    assert "$2.99 short of a $19.99 entry" in text
    assert "PANW (pullback 0.74), SNOW (pullback 0.73)" in text
    assert "$40.64 settles Tue Sep 17" in text

    assert cash_alert_message(result.model_copy(update={"cash_alert": False})) is None


def test_live_report_prints_the_whole_cash_ledger():
    day = EngineDay(
        engine="live",
        last_session=SESSION,
        capital=D("103.01"),
        mode=EngineMode.LIVE,
        cash_settled=D("-20.6384"),
        cash_unsettled=D("40.6377"),
        cash_bridge=D("57"),
        cash_bridged=D("20.6384"),
        cash_buying_power=D("19.9993"),
        cash_next_settle=date(2024, 9, 17),
        cash_next_amount=D("40.6377"),
        cash_block=CashBlock(
            free_slots=1,
            entry_cost=D("20"),
            buying_power=D("19.9993"),
            settles_on=date(2024, 9, 17),
            settles_amount=D("40.6377"),
        ),
        cash_refused_today=[_refusal("PANW", 0.74)],
    )
    text = live_report_message(LiveReport(on=SESSION, day=day))
    assert "Buying power $19.99</b>" in text  # floored, never $20.00
    assert "capital $103.01 · settled -$20.64 · unsettled $40.64" in text
    assert "($40.64 settles Tue Sep 17)" in text
    assert "bridge $57.00 ($20.64 in use)" in text
    assert "cash-blocked: 1 free slot, $0.01 short" in text
    assert "Passed over for cash today: PANW (pullback 0.74)" in text
