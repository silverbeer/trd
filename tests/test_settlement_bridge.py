"""The settlement bridge (SB-1445): spare broker cash that fronts sale proceeds
still settling, so a refill does not wait out T+1.

Buying power is `settled + min(bridge, unsettled)`. The bridge never lets the
engine spend money it has not made — only spend it a business day sooner — and
it is never part of the capital, so worth and return read the capital alone.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.db.connection import connect
from trd.engine.settlement import ledger, unsettled_funding
from trd.errors import TrdError
from trd.models import Side
from trd.repos import InstrumentRepo, PriceRepo
from trd.services import BacktestService, EngineService
from trd.services.backup import export_data, restore_data

from .conftest import FakeProvider
from .test_backtest import run
from .test_settlement import _exit_and_entry_share_a_day, _two_names

runner = CliRunner()

D = Decimal

MONDAY = date(2026, 10, 19)
TUESDAY = date(2026, 10, 20)

# Fully invested, then a 950 sale on Monday: nothing settled, 950 on its way.
FLOWS = [
    (MONDAY, Side.BUY, D("10"), D("100"), D(0)),
    (MONDAY, Side.SELL, D("10"), D("95"), D(0)),
]


# ---------------------------------------------------------------------- ledger


def test_no_bridge_leaves_buying_power_at_settled():
    cash = ledger(D("1000"), FLOWS, MONDAY)
    assert cash.bridge == 0
    assert cash.buying_power == cash.settled == D(0)
    assert cash.bridged == 0


def test_the_bridge_fronts_proceeds_still_settling():
    cash = ledger(D("1000"), FLOWS, MONDAY, D("500"))
    assert cash.settled == D(0)
    assert cash.buying_power == D("500")


def test_the_bridge_never_fronts_more_than_is_settling():
    """A bridge bigger than the unsettled proceeds admits only what is on its way:
    the engine cannot spend money it has not made."""
    cash = ledger(D("1000"), FLOWS, MONDAY, D("5000"))
    assert cash.buying_power == D("950")


def test_a_bridged_buy_goes_below_zero_and_settles_back():
    cash = ledger(D("1000"), FLOWS, MONDAY, D("500")).spend(D("400"))
    assert cash.settled == D("-400")
    assert cash.bridged == D("400")
    assert cash.buying_power == D("100")  # -400 + min(500, 950)

    flows = [*FLOWS, (MONDAY, Side.BUY, D("4"), D("100"), D(0))]
    tuesday = ledger(D("1000"), flows, TUESDAY, D("500"))
    assert tuesday.settled == D("550")  # 950 settled, 400 of it repaid the bridge
    assert tuesday.bridged == 0
    assert tuesday.buying_power == D("550")


def test_the_shortfall_names_the_bridge():
    cash = ledger(D("1000"), FLOWS, MONDAY, D("100"))
    reason = cash.shortfall("AAA", D("500"))
    assert "$100.00 bridged" in reason
    assert "($100.00 bridge, $0.00 in use)" in reason
    assert "settles 2026-10-20" in reason


def test_ledger_json_carries_the_bridge():
    doc = json.loads(ledger(D("1000"), FLOWS, MONDAY, D("200")).model_dump_json())
    assert D(doc["bridge"]) == D("200")
    assert D(doc["buying_power"]) == D("200")
    assert D(doc["bridged"]) == D(0)


def test_a_bridged_buy_is_not_a_good_faith_violation():
    """At the broker the bridged part is the owner's own settled cash, so the
    buy was paid for in full and selling it the same day sells nothing unpaid."""
    assert unsettled_funding(D("1000"), FLOWS, MONDAY, D("400")) == TUESDAY
    assert unsettled_funding(D("1000"), FLOWS, MONDAY, D("400"), D("500")) is None
    # Beyond the bridge the remainder is still unsettled money.
    assert unsettled_funding(D("1000"), FLOWS, MONDAY, D("400"), D("100")) == TUESDAY


# ---------------------------------------------------------------------- engine


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def test_a_bridge_lets_the_refill_go_through_the_same_day(engine, provider, conn):
    """The SB-1445 case: the exit's proceeds are unsettled, and the bridge fronts
    them, so the other name's entry is taken in the same pass."""
    _two_names(conn, provider)
    engine.init(
        symbols=["AAA", "BBB"],
        strategies=["momentum"],
        position_size=D("5000"),
        max_positions=1,
        capital=D("6000"),
    )
    engine.set_bridge(D("6000"))
    opened = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(opened.opened) == 1
    held = opened.opened[0].symbol

    position = engine.position_rows(open_only=True)[0].position
    provider.add_symbol(held, price=str(float(position.stop_price) * 0.99))
    refill = engine.scan(at=datetime(2024, 9, 16, 14, 0))
    assert len(refill.closed) == 1
    assert len(refill.opened) == 1
    assert not any("T+1" in s for s in refill.skipped)

    cash = engine.cash(today=date(2024, 9, 16))
    assert cash is not None and cash.bridged > 0
    assert cash.buying_power >= 0


def test_setting_the_bridge_touches_nothing_else(engine, provider):
    provider.add_symbol("AAA", price="100")
    before = engine.init(symbols=["AAA"], capital=D("100"), max_entries_per_day=2)[0]
    after = engine.set_bridge(D("100"))
    assert after.bridge == D("100")
    assert after.model_dump(exclude={"id", "bridge"}) == before.model_dump(exclude={"id", "bridge"})
    # The capital setter carries the bridge across a re-base.
    assert engine.set_capital(D("150")).bridge == D("100")


def test_the_bridge_is_not_capital(engine, provider):
    """Worth and return read the capital; the bridge only changes what an entry
    may spend."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], capital=D("100"))
    engine.set_bridge(D("100"))
    status = engine.status()
    assert status.capital == D("100")
    assert status.cash_bridge == D("100")
    assert status.cash_settled == status.cash_buying_power == D("100")  # nothing settling
    assert status.cash_bridged == 0


def test_a_bridge_needs_the_ledger_on(engine, provider):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"])
    with pytest.raises(TrdError, match="ledger is off"):
        engine.set_bridge(D("100"))


def test_a_non_positive_bridge_is_refused(engine, provider):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], capital=D("100"))
    with pytest.raises(TrdError, match="Bridge must be positive"):
        engine.set_bridge(D("0"))


def test_switching_the_ledger_off_clears_the_bridge(engine, provider):
    """Otherwise a later --set would bring back a bridge nobody re-stated."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], capital=D("100"))
    engine.set_bridge(D("100"))
    assert engine.set_capital(None).bridge is None
    assert engine.set_capital(D("100")).bridge is None


def test_backup_round_trips_the_bridge(tmp_path: Path, engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], capital=D("100"))
    engine.set_bridge(D("75.5"))
    data = json.loads(json.dumps(export_data(conn), default=str))
    assert data["engine"]["config"]["bridge"] == "75.500000"

    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    assert EngineService(fresh, provider).config().bridge == D("75.5")


def test_a_backup_without_a_bridge_restores_with_none(tmp_path: Path, engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], capital=D("100"))
    data = json.loads(json.dumps(export_data(conn), default=str))
    del data["engine"]["config"]["bridge"]  # the shape of every backup before v12
    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    assert EngineService(fresh, provider).config().bridge is None


# ------------------------------------------------------------------------- CLI


def test_engine_cash_bridge_cli(cli_env: FakeProvider):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert runner.invoke(app, ["engine", "init", "--symbols", "AAA"]).exit_code == 0

    refused = runner.invoke(app, ["engine", "cash", "--bridge", "50", "--json"])
    assert refused.exit_code == 1
    assert "ledger is off" in json.loads(refused.output)["message"]

    on = runner.invoke(app, ["engine", "cash", "--set", "100", "--bridge", "50", "--json"])
    assert on.exit_code == 0, on.output
    doc = json.loads(on.output)
    assert D(doc["capital"]) == D("100") and D(doc["bridge"]) == D("50")

    human = runner.invoke(app, ["engine", "cash"])
    assert human.exit_code == 0 and "bridge" in human.output and "buying power" in human.output

    status = json.loads(runner.invoke(app, ["engine", "status", "--json"]).output)
    assert D(status["cash_bridge"]) == D("50")

    both = runner.invoke(app, ["engine", "cash", "--bridge", "5", "--no-bridge", "--json"])
    assert both.exit_code == 1

    cleared = json.loads(runner.invoke(app, ["engine", "cash", "--no-bridge", "--json"]).output)
    assert D(cleared["bridge"]) == 0
    assert D(cleared["capital"]) == D("100")


# -------------------------------------------------------------------- backtest


def test_backtest_bridge_takes_the_same_day_refill():
    """1,500 capital: 500 left after AAA, its ~886 of proceeds settling. BBB's
    ~1,000 is more than settled and less than settled + the bridged proceeds."""
    series = _exit_and_entry_share_a_day()
    blocked = run(series, max_positions=1, settlement=True, capital=D("1500"))
    bridged = run(series, max_positions=1, settlement=True, capital=D("1500"), bridge=D("5000"))
    assert blocked.cash_blocked == 1 and blocked.open_at_end == 0
    assert bridged.cash_blocked == 0 and bridged.open_at_end == 1
    assert bridged.bridge == D("5000")


def test_backtest_bridge_never_fronts_more_than_is_settling():
    """At 1,000 capital the loss leaves ~886 settling against a ~1,000 entry: no
    bridge, however large, lets the engine spend money it did not make."""
    series = _exit_and_entry_share_a_day()
    huge = run(series, max_positions=1, settlement=True, bridge=D("1000000"))
    assert huge.cash_blocked == 1


def test_backtest_service_inherits_the_bridge(conn: duckdb.DuckDBPyConnection):
    provider = FakeProvider()
    for symbol in ("AAA", "BBB"):
        provider.add_symbol(symbol, price="101.00")
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA", "BBB"], strategies=["breakout"], max_positions=1, capital=D("1500"))
    engine.set_bridge(D("1000"))
    for symbol, bars in _exit_and_entry_share_a_day().items():
        instrument = InstrumentRepo(conn).get_by_symbol(symbol)
        assert instrument is not None
        PriceRepo(conn).upsert_daily(instrument.id, bars)

    inherited = BacktestService(conn).run(blackout=False)
    assert inherited.bridge == D("1000") and inherited.cash_blocked == 0

    none = BacktestService(conn).run(blackout=False, bridge=D(0))
    assert none.cash_blocked == 1
