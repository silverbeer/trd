"""The T+1 settled-cash ledger (SB-1368).

A cash account cannot spend a sale's proceeds until they settle one business day
later. The engine refilled every freed slot in the same pass — 86 of 112 entries
on the swing paper book would have needed unsettled money — so a live engine and
its paper twin would have traded different books after the first exit. These
tests pin the arithmetic, and that the scan and the backtest refuse the same
refill.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.db.connection import apply_migrations, connect
from trd.engine.settlement import ledger, settles_on
from trd.errors import TrdError
from trd.models import AccountType, Side
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo
from trd.repos.engine import EngineConfigRepo
from trd.services import BacktestService, EngineService

from .conftest import FakeProvider
from .test_backtest import bar, run
from .test_engine import make_bars, seed, uptrend

runner = CliRunner()

D = Decimal

# ------------------------------------------------------------------ settles_on


def test_a_friday_sale_settles_monday():
    assert settles_on(date(2026, 10, 16)) == date(2026, 10, 19)


def test_a_monday_sale_settles_tuesday():
    assert settles_on(date(2026, 10, 19)) == date(2026, 10, 20)


# ---------------------------------------------------------------------- ledger


def test_same_day_proceeds_are_unsettled():
    """Bought Monday, sold Monday: the cash left on the buy, the sale's money is
    the account's but not buying power until Tuesday."""
    monday = date(2026, 10, 19)
    flows = [
        (monday, Side.BUY, D("10"), D("100"), D(0)),
        (monday, Side.SELL, D("10"), D("95"), D(0)),
    ]
    cash = ledger(D("1500"), flows, monday)
    assert cash.settled == D("500")
    assert cash.unsettled == D("950")
    assert cash.pending == [(date(2026, 10, 20), D("950"))]
    assert cash.next_settle == (date(2026, 10, 20), D("950"))

    tuesday = ledger(D("1500"), flows, date(2026, 10, 20))
    assert tuesday.settled == D("1450")
    assert tuesday.unsettled == D(0)
    assert tuesday.pending == []


def test_fees_cost_on_both_sides():
    """A buy costs price x qty PLUS fees; a sale brings in price x qty MINUS fees."""
    monday = date(2026, 10, 19)
    flows = [
        (monday, Side.BUY, D("2"), D("100"), D("1.50")),
        (monday, Side.SELL, D("2"), D("110"), D("1.25")),
    ]
    cash = ledger(D("1000"), flows, date(2026, 10, 20))
    assert cash.settled == D("1000") - D("201.50") + D("218.75")
    assert cash.unsettled == D(0)


def test_a_partial_exit_is_just_a_sell():
    """A trim settles on its own date, independently of the rest of the trade."""
    flows = [
        (date(2026, 10, 15), Side.BUY, D("10"), D("100"), D(0)),
        (date(2026, 10, 15), Side.SELL, D("4"), D("110"), D(0)),  # Thu trim
        (date(2026, 10, 16), Side.SELL, D("6"), D("90"), D(0)),  # Fri exit
    ]
    cash = ledger(D("1000"), flows, date(2026, 10, 16))
    assert cash.settled == D("440")  # Thursday's 440 settled Friday
    assert cash.pending == [(date(2026, 10, 19), D("540"))]  # Friday's waits for Monday


def test_proceeds_are_grouped_and_ordered_by_settle_date():
    flows = [
        (date(2026, 10, 19), Side.SELL, D("1"), D("20"), D(0)),
        (date(2026, 10, 16), Side.SELL, D("1"), D("10"), D(0)),
        (date(2026, 10, 19), Side.SELL, D("1"), D("5"), D(0)),
    ]
    cash = ledger(D("0"), flows, date(2026, 10, 16))
    # Flows after the as-of day are not part of it.
    assert cash.pending == [(date(2026, 10, 19), D("10"))]
    later = ledger(D("0"), flows, date(2026, 10, 19))
    assert later.settled == D("10")
    assert later.pending == [(date(2026, 10, 20), D("25"))]


def test_the_shortfall_names_the_settle_date():
    cash = ledger(
        D("100"),
        [
            (date(2026, 10, 20), Side.BUY, D("1"), D("95.90"), D(0)),
            (date(2026, 10, 20), Side.SELL, D("1"), D("18.30"), D(0)),
        ],
        date(2026, 10, 20),
    )
    assert cash.shortfall("AAPL", D("20")) == (
        "AAPL: $20.00 needed, $4.10 settled — $18.30 from earlier exits settles "
        "2026-10-21 (T+1, cash account)"
    )


# ------------------------------------------------------------------- live scan


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _two_names(conn, provider):
    for symbol in ("AAA", "BBB"):
        bars = make_bars(uptrend())
        provider.add_symbol(symbol, price=str(float(bars[-1].close)))
        seed(conn, symbol, bars)
    return bars


def test_a_refill_waits_for_the_exit_proceeds_to_settle(engine, provider, conn):
    """One slot, capital for a little more than one position. The exit frees the
    slot, but its proceeds are unsettled, so the other name's signal is recorded
    and not taken. The next business day the same entry goes through."""
    bars = _two_names(conn, provider)
    engine.init(
        symbols=["AAA", "BBB"],
        strategies=["momentum"],
        position_size=D("5000"),
        max_positions=1,
        capital=D("6000"),
    )
    monday = datetime(2024, 9, 16, 10, 0)
    opened = engine.scan(at=monday)
    assert len(opened.opened) == 1
    held = opened.opened[0].symbol
    other = "BBB" if held == "AAA" else "AAA"

    position = engine.position_rows(open_only=True)[0].position
    provider.add_symbol(held, price=str(float(position.stop_price) * 0.99))
    refill = engine.scan(at=datetime(2024, 9, 16, 14, 0))
    assert len(refill.closed) == 1
    assert refill.opened == []
    blocked = [s for s in refill.skipped if s.startswith(f"{other}: $")]
    assert len(blocked) == 1, refill.skipped
    assert "settles 2024-09-17 (T+1, cash account)" in blocked[0]

    stored = [r.signal for r in engine.signal_rows() if r.instrument.symbol == other]
    assert stored and not any(s.acted for s in stored)

    cash = engine.cash(today=date(2024, 9, 16))
    assert cash is not None
    assert cash.unsettled > 0
    assert cash.next_settle is not None and cash.next_settle[0] == date(2024, 9, 17)

    # Tuesday: the proceeds have settled. Quotes nudged off the stored close, so
    # the scanner does not read them as "not printed yet".
    for symbol in ("AAA", "BBB"):
        provider.add_symbol(symbol, price=str(float(bars[-1].close) * 1.002))
    tuesday = engine.scan(at=datetime(2024, 9, 17, 10, 0))
    assert len(tuesday.opened) == 1
    assert not any("T+1" in s for s in tuesday.skipped)


def test_no_capital_means_no_ledger(engine, provider, conn):
    """Every engine predating migration 026: same full-book refill as before."""
    _two_names(conn, provider)
    engine.init(
        symbols=["AAA", "BBB"],
        strategies=["momentum"],
        position_size=D("5000"),
        max_positions=1,
    )
    assert engine.config().capital is None
    assert engine.cash() is None
    engine.scan(at=datetime(2024, 9, 16, 10, 0))
    position = engine.position_rows(open_only=True)[0].position
    held = engine.position_rows(open_only=True)[0].instrument.symbol
    provider.add_symbol(held, price=str(float(position.stop_price) * 0.99))
    refill = engine.scan(at=datetime(2024, 9, 16, 14, 0))
    assert len(refill.closed) == 1
    assert len(refill.opened) == 1
    assert engine.status().capital is None
    assert engine.status().cash_settled is None


def test_status_carries_the_cash_line(engine, provider, conn):
    _two_names(conn, provider)
    engine.init(symbols=["AAA", "BBB"], strategies=["momentum"], capital=D("2500"))
    status = engine.status()
    assert status.capital == D("2500")
    assert status.cash_settled == D("2500")
    assert status.cash_unsettled == D(0)
    assert status.cash_next_settle is None


def test_a_non_positive_capital_is_refused(engine, provider):
    provider.add_symbol("AAA", price="100")
    with pytest.raises(TrdError, match="Capital must be positive"):
        engine.init(symbols=["AAA"], capital=D(0))


# ---------------------------------------------------------- set_capital / cash


def test_setting_capital_preserves_every_other_field(engine, provider):
    provider.add_symbol("AAA", price="100")
    provider.add_symbol("BBB", price="100")
    before, _, universe = engine.init(
        symbols=["AAA", "BBB"],
        strategies=["momentum", "breakout"],
        exit_params={"scale_out_pct": 50.0, "flat_at_minute": 1555.0},
        timeframe="5m",
        max_entries_per_day=3,
        position_size=D("250"),
        max_positions=4,
        earnings_blackout_days=1,
    )
    after = engine.set_capital(D("5000"))
    assert after.capital == D("5000")
    assert after.model_dump(exclude={"id", "capital"}) == before.model_dump(
        exclude={"id", "capital"}
    )
    assert [i.symbol for i in engine.universe()] == universe

    off = engine.set_capital(None)
    assert off.capital is None
    assert off.model_dump(exclude={"id", "capital"}) == before.model_dump(exclude={"id", "capital"})


def test_capital_below_what_the_history_spent_is_refused(engine, provider, conn):
    """The ledger replays every fill from the starting cash, so a capital smaller
    than what the account already spent would open negative and refuse every
    entry with no visible reason."""
    _two_names(conn, provider)
    engine.init(symbols=["AAA", "BBB"], strategies=["momentum"], position_size=D("5000"))
    assert len(engine.scan(at=datetime(2024, 9, 16, 10, 0)).opened) >= 1
    with pytest.raises(TrdError, match="already spent"):
        engine.set_capital(D("100"))
    assert engine.config().capital is None
    assert engine.set_capital(D("20000")).capital == D("20000")


def test_engine_cash_cli(cli_env: FakeProvider):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert (
        runner.invoke(app, ["engine", "init", "--symbols", "AAA", "--max-entries-per-day", "2"])
    ).exit_code == 0

    off = runner.invoke(app, ["engine", "cash", "--json"])
    assert off.exit_code == 0, off.output
    assert json.loads(off.output) == {"capital": None}

    on = runner.invoke(app, ["engine", "cash", "--set", "1200", "--json"])
    assert on.exit_code == 0, on.output
    doc = json.loads(on.output)
    assert D(doc["settled"]) == D("1200")
    assert doc["pending"] == [] and doc["next_settle"] is None

    human = runner.invoke(app, ["engine", "cash"])
    assert human.exit_code == 0 and "settled" in human.output

    status = json.loads(runner.invoke(app, ["engine", "status", "--json"]).output)
    assert D(status["cash_settled"]) == D("1200")
    assert status["max_entries_per_day"] == 2

    both = runner.invoke(app, ["engine", "cash", "--set", "5", "--off", "--json"])
    assert both.exit_code == 1
    assert set(json.loads(both.output)) == {"error", "message"}

    bad = runner.invoke(app, ["engine", "cash", "--set", "-5", "--json"])
    assert bad.exit_code == 1
    assert "Capital must be positive" in json.loads(bad.output)["message"]

    assert json.loads(runner.invoke(app, ["engine", "cash", "--off", "--json"]).output) == {
        "capital": None
    }


# -------------------------------------------------------------------- backtest


def _exit_and_entry_share_a_day():
    """AAA breaks out and fills on bar 60, gaps through its stop on bar 61; BBB
    breaks out on bar 61. One slot: the exit frees it, and BBB wants it."""
    aaa = make_bars([100.0] * 60 + [101.0], volumes=[1_000_000] * 60 + [3_000_000])
    crash_day = aaa[-1].date + timedelta(days=1)
    aaa = [*aaa, bar(crash_day, 90.0, 90.5, 89.0, 89.5)]
    bbb = make_bars([100.0] * 61 + [101.0], volumes=[1_000_000] * 61 + [3_000_000])
    return {"AAA": aaa, "BBB": bbb}


def test_backtest_without_settlement_is_unchanged():
    series = _exit_and_entry_share_a_day()
    default = run(series, max_positions=1)
    explicit = run(series, max_positions=1, settlement=False)
    assert default.model_dump_json() == explicit.model_dump_json()
    assert default.settlement is False and default.cash_blocked == 0
    # The refill happens: AAA closed, BBB taken on the same bar.
    assert [t.symbol for t in default.trades] == ["AAA"]
    assert default.open_at_end == 1


def test_backtest_settlement_blocks_the_same_day_refill():
    series = _exit_and_entry_share_a_day()
    off = run(series, max_positions=1)
    on = run(series, max_positions=1, settlement=True)
    assert on.settlement is True
    assert on.cash_blocked == 1
    assert on.open_at_end == 0
    # Pending proceeds count toward equity, so the curve does not dip on timing:
    # BBB marked at its own fill price is worth exactly the cash it cost.
    assert on.end_value == off.end_value


def test_settlement_changes_nothing_when_no_exit_funds_an_entry():
    series = {"AAA": _exit_and_entry_share_a_day()["AAA"]}
    off = run(series, max_positions=1)
    on = run(series, max_positions=1, settlement=True)
    assert on.cash_blocked == 0
    assert [t.model_dump() for t in on.trades] == [t.model_dump() for t in off.trades]
    assert on.equity == off.equity


def test_backtest_service_inherits_the_engines_capital(conn: duckdb.DuckDBPyConnection):
    provider = FakeProvider()
    for symbol in ("AAA", "BBB"):
        provider.add_symbol(symbol, price="101.00")
    engine = EngineService(conn, provider)
    engine.init(
        symbols=["AAA", "BBB"],
        strategies=["breakout"],
        max_positions=1,
        capital=D("1000"),
    )
    for symbol, bars in _exit_and_entry_share_a_day().items():
        instrument = InstrumentRepo(conn).get_by_symbol(symbol)
        assert instrument is not None
        PriceRepo(conn).upsert_daily(instrument.id, bars)

    inherited = BacktestService(conn).run(blackout=False)
    assert inherited.settlement is True
    assert inherited.start_value == D("1000")
    assert inherited.cash_blocked == 1

    overridden = BacktestService(conn).run(blackout=False, settlement=False)
    assert overridden.settlement is False
    assert overridden.cash_blocked == 0
    assert overridden.open_at_end == 1


# ------------------------------------------------------------------- migration


def test_a_row_written_before_migration_026_reads_capital_none(tmp_path):
    """Roll the database back to before 026, write a config the old way, then
    migrate: the row must come back with the ledger off."""
    db = tmp_path / "old.duckdb"
    conn = connect(db)
    conn.execute("ALTER TABLE engine_config DROP COLUMN capital")
    conn.execute("DELETE FROM schema_migrations WHERE filename = '026_engine_capital.sql'")
    account = AccountRepo(conn).create("engine-sim", AccountType.SIMULATION)
    conn.execute(
        """
        INSERT INTO engine_config
            (account_id, watchlist, position_size, max_positions, strategies, exit_params,
             earnings_blackout_days, sizing_mode, timeframe, max_entries_per_day)
        VALUES (?, 'engine', 1000, 5, '["momentum"]', '{}', 3, 'exposure', '1d', 0)
        """,
        [account.id],
    )
    assert apply_migrations(conn) == ["026_engine_capital.sql"]
    config = EngineConfigRepo(conn).get()
    assert config is not None
    assert config.capital is None
    assert config.strategies == ["momentum"]
