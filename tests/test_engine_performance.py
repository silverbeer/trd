"""`trd engine performance` (SB-1447): an engine's worth, session by session,
from its capital — cash replayed through every fill plus holdings at the close.

Synthetic figures throughout. The property that matters most is the one the
equity curve lacks: a sale swaps shares for the same dollars and never moves
worth.
"""

import json
from datetime import date, datetime
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.errors import TrdError
from trd.models import Side
from trd.repos import InstrumentRepo, PriceRepo, TransactionRepo
from trd.services import EngineService
from trd.services.engine_performance import engine_performance
from trd.services.equity_curve import EquityCurveService

from .conftest import FakeProvider
from .test_backtest import bar

runner = CliRunner()

D = Decimal

MON, TUE, WED = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)
FRI_BEFORE = date(2026, 2, 27)


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    for symbol in ("AAA", "BBB", "SPY"):
        provider.add_symbol(symbol, price="100")
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA", "BBB"], capital=D("1000"))
    return engine


def _closes(conn, symbol: str, closes: dict[date, float]) -> int:
    instrument = InstrumentRepo(conn).get_by_symbol(symbol)
    if instrument is None:
        instrument = InstrumentRepo(conn).insert(_info(symbol))
    PriceRepo(conn).upsert_daily(instrument.id, [bar(d, c, c, c, c) for d, c in closes.items()])
    return instrument.id


def _info(symbol: str):
    provider = FakeProvider()
    provider.add_symbol(symbol, price="100")
    return provider.get_info(symbol)


def _fill(conn, engine, symbol, side, qty, price, when):
    instrument = InstrumentRepo(conn).get_by_symbol(symbol)
    assert instrument is not None
    TransactionRepo(conn).insert(
        engine.account().id, instrument.id, side, D(qty), D(price), D(0), when
    )


@pytest.fixture
def book(conn, engine):
    """Monday: buy 5 AAA @100 and 4 BBB @100 (900 of 1000). AAA closes 110,
    BBB 95. Tuesday: AAA closes 120, sold all at 120. Wednesday: BBB 105."""
    _closes(conn, "AAA", {MON: 110, TUE: 120, WED: 125})
    _closes(conn, "BBB", {MON: 95, TUE: 90, WED: 105})
    _fill(conn, engine, "AAA", Side.BUY, "5", "100", datetime(2026, 3, 2, 9, 40))
    _fill(conn, engine, "BBB", Side.BUY, "4", "100", datetime(2026, 3, 2, 9, 41))
    _fill(conn, engine, "AAA", Side.SELL, "5", "120", datetime(2026, 3, 3, 15, 0))
    return engine


def test_worth_is_cash_plus_holdings_at_the_close(book):
    perf = engine_performance(book, today=date(2026, 3, 10))
    mon, tue, wed = perf.sessions
    assert mon.worth == D("1030")  # 100 cash + 5 x 110 + 4 x 95
    assert tue.worth == D("1060")  # 700 cash + 4 x 90
    assert wed.worth == D("1120")  # 700 cash + 4 x 105
    assert (mon.day_change, tue.day_change, wed.day_change) == (D("30"), D("30"), D("60"))
    assert wed.since_start_pct == D("12")
    assert mon.held == ["AAA", "BBB"] and tue.held == ["BBB"]


def test_a_sale_never_moves_worth(book):
    """Tuesday's sell is at Tuesday's close: shares out, the same dollars in."""
    tue = engine_performance(book, today=date(2026, 3, 10)).sessions[1]
    assert tue.worth == D("700") + 4 * D("90")
    assert tue.sells == 1 and tue.realized == D("100")  # 5 x (120 - 100)


def test_the_summary_splits_booked_from_open(book):
    perf = engine_performance(book, today=date(2026, 3, 10))
    assert perf.worth == D("1120") and perf.net == D("120")
    assert perf.return_pct == D("12")
    assert perf.realized == D("100")
    assert perf.unrealized == D("20")  # BBB 4 x (105 - 100)
    assert perf.best is not None and perf.best.date == WED


def test_drawdown_reads_from_the_running_peak(conn, engine):
    _closes(conn, "AAA", {MON: 120, TUE: 90, WED: 100})
    _fill(conn, engine, "AAA", Side.BUY, "10", "100", datetime(2026, 3, 2, 9, 40))
    perf = engine_performance(engine, today=date(2026, 3, 10))
    assert [s.worth for s in perf.sessions] == [D("1200"), D("900"), D("1000")]
    assert perf.sessions[0].drawdown_pct == 0
    assert perf.sessions[1].drawdown_pct == D("-25")
    assert perf.max_drawdown_pct == D("-25")


def test_spy_counts_from_the_close_before_the_first_fill(conn, book):
    _closes(conn, "SPY", {FRI_BEFORE: 500, MON: 505, TUE: 510, WED: 495})
    perf = engine_performance(book, today=date(2026, 3, 10))
    assert [s.benchmark_since_start_pct for s in perf.sessions] == [D("1"), D("2"), D("-1")]
    assert perf.benchmark_return_pct == D("-1")


def test_no_spy_bars_reads_unknown(book):
    perf = engine_performance(book, today=date(2026, 3, 10))
    assert perf.benchmark_return_pct is None


def test_todays_session_is_provisional(book):
    perf = engine_performance(book, today=WED)
    assert [s.provisional for s in perf.sessions] == [False, False, True]


def test_no_capital_is_refused(conn, provider):
    provider.add_symbol("AAA", price="100")
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA"])
    with pytest.raises(TrdError, match="no capital"):
        engine_performance(engine)


def test_no_fills_is_refused(engine):
    with pytest.raises(TrdError, match="No fills yet"):
        engine_performance(engine)


def test_equity_flags_an_engine_account(conn, book):
    curve = EquityCurveService(conn).curve(book.account().name)
    assert curve.engine_account is True


# ------------------------------------------------------------------------- CLI


def test_engine_performance_cli(cli_env: FakeProvider):
    from trd.config import get_settings
    from trd.db.connection import connect

    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert (
        runner.invoke(app, ["engine", "init", "--symbols", "AAA", "--capital", "1000"]).exit_code
        == 0
    )
    conn = connect(get_settings().db_path)
    engine = EngineService(conn, cli_env)
    _closes(conn, "AAA", {MON: 110})
    _fill(conn, engine, "AAA", Side.BUY, "5", "100", datetime(2026, 3, 2, 9, 40))
    conn.close()

    doc = runner.invoke(app, ["engine", "performance", "--json"])
    assert doc.exit_code == 0, doc.output
    data = json.loads(doc.output)
    assert D(data["worth"]) == D("1050") and D(data["capital"]) == D("1000")
    assert data["sessions"][0]["held"] == ["AAA"]

    human = runner.invoke(app, ["engine", "perf"])
    assert human.exit_code == 0 and "Since the start" in human.output

    equity = runner.invoke(app, ["equity", "--account", "engine-sim"])
    assert "trd engine performance" in equity.output
