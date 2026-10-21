"""Live engine mode, phase 1a (SB-1374): a live scan records order intents.

A paper engine fills its own decisions. A live one trades a real account, where
the fill is the broker's and arrives later — so where paper writes a txn and
opens a position, live writes an `order_intent` and leaves the book alone. These
tests pin what an open intent must still claim (a slot, the name, its cash, the
right to exit a position once) and that paper mode is untouched.
"""

import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.config import get_settings
from trd.db.connection import apply_migrations, connect
from trd.errors import EnginePositionConflictError, TrdError
from trd.models import AccountType, EngineMode, IntentStatus, Side
from trd.repos import AccountRepo, InstrumentRepo, OrderIntentRepo, TransactionRepo
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo
from trd.services import EngineService, PortfolioService
from trd.services.backup import export_data, restore_data
from trd.services.engine import scan_events, stop_distance

from .conftest import FakeProvider
from .test_engine import make_bars, seed, uptrend

runner = CliRunner()
D = Decimal

MONDAY = datetime(2024, 9, 16, 10, 0)
CAPITAL = D("100000")


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _names(conn, provider, *symbols: str) -> None:
    for symbol in symbols:
        bars = make_bars(uptrend())
        provider.add_symbol(symbol, price=str(float(bars[-1].close)))
        seed(conn, symbol, bars)


def _live(
    engine: EngineService,
    symbols: list[str],
    capital: Decimal | None = CAPITAL,
    timeframe: str = "1d",
    exit_params: dict[str, float] | None = None,
    max_positions: int = 5,
    max_entries_per_day: int = 0,
):
    return engine.init(
        account_name="brokerage",
        symbols=symbols,
        strategies=["momentum"],
        position_size=D("5000"),
        capital=capital,
        timeframe=timeframe,
        exit_params=exit_params,
        max_positions=max_positions,
        max_entries_per_day=max_entries_per_day,
        mode=EngineMode.LIVE,
    )


def _intents(conn, engine: EngineService):
    return [i for i, _ in OrderIntentRepo(conn).list_for(engine.account().id)]


# ------------------------------------------------------------------- migration


def test_migration_027_applies_on_a_fresh_database(conn):
    applied = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    assert "027_live_engine_order_intent.sql" in applied
    columns = {r[0] for r in conn.execute("DESCRIBE order_intent").fetchall()}
    assert {
        "ref_id",
        "approval_id",
        "broker_order_id",
        "status",
        "stop_price",
        "atr_at_entry",
        "position_id",
        "signal_id",
        "filled_quantity",
        "fill_price",
        "fees",
    } <= columns
    assert "mode" in {r[0] for r in conn.execute("DESCRIBE engine_config").fetchall()}


def test_a_database_at_026_migrates_and_reads_paper(tmp_path: Path):
    """Roll back to 026, write a config the old way, migrate: the engine that
    predates live mode must come back as paper."""
    conn = connect(tmp_path / "old.duckdb")
    conn.execute("DROP TABLE order_intent")
    conn.execute("DROP SEQUENCE order_intent_id_seq")
    conn.execute("ALTER TABLE engine_config DROP COLUMN mode")
    conn.execute(
        "DELETE FROM schema_migrations WHERE filename = '027_live_engine_order_intent.sql'"
    )
    account = AccountRepo(conn).create("engine-sim", AccountType.SIMULATION)
    conn.execute(
        """
        INSERT INTO engine_config
            (account_id, watchlist, position_size, max_positions, strategies, exit_params,
             earnings_blackout_days, sizing_mode, timeframe, max_entries_per_day, capital)
        VALUES (?, 'engine', 1000, 5, '["momentum"]', '{}', 3, 'exposure', '1d', 0, NULL)
        """,
        [account.id],
    )
    assert apply_migrations(conn) == ["027_live_engine_order_intent.sql"]
    config = EngineConfigRepo(conn).get()
    assert config is not None
    assert config.mode == EngineMode.PAPER
    assert conn.execute("SELECT count(*) FROM order_intent").fetchone() == (0,)


# ------------------------------------------------------------------------ init


def test_live_init_creates_a_real_account(engine, provider):
    provider.add_symbol("AAA", price="100")
    config, account, _ = _live(engine, ["AAA"])
    assert config.mode == EngineMode.LIVE and config.is_live
    assert account.type == AccountType.REAL
    assert engine.status().mode == EngineMode.LIVE


def test_live_init_without_capital_is_refused(engine, conn, provider):
    provider.add_symbol("AAA", price="100")
    with pytest.raises(TrdError, match="needs --capital"):
        _live(engine, ["AAA"], capital=None)
    # Refused before anything was created: no half-made real account.
    assert AccountRepo(conn).get_by_name("brokerage") is None


def test_live_init_on_the_default_paper_account_name_is_refused(engine, conn, provider):
    """The default name says 'sim'; a real-money account is named on purpose."""
    provider.add_symbol("AAA", price="100")
    with pytest.raises(TrdError, match="name its account with --account"):
        engine.init(symbols=["AAA"], mode=EngineMode.LIVE, capital=Decimal(100))
    assert AccountRepo(conn).get_by_name("engine-sim") is None


def test_live_init_on_a_simulation_account_is_refused(engine, conn, provider):
    provider.add_symbol("AAA", price="100")
    AccountRepo(conn).create("brokerage", AccountType.SIMULATION)
    with pytest.raises(TrdError, match="simulation account"):
        _live(engine, ["AAA"])


def test_live_init_on_intraday_bars_is_refused(engine, provider):
    provider.add_symbol("AAA", price="100")
    with pytest.raises(TrdError, match="daily bars"):
        _live(engine, ["AAA"], timeframe="5m")


def test_live_init_with_flat_at_is_refused(engine, provider):
    """The live reason, not the generic day-mode one — whose advice is to pass
    an intraday timeframe, which a live engine is also refused."""
    provider.add_symbol("AAA", price="100")
    with pytest.raises(TrdError, match="cannot use --flat-at"):
        _live(engine, ["AAA"], exit_params={"flat_at_minute": 1555.0})


def test_paper_init_on_a_real_account_is_refused(engine, conn):
    AccountRepo(conn).create("brokerage", AccountType.REAL)
    with pytest.raises(TrdError, match="pass --live"):
        engine.init(account_name="brokerage", symbols=[])


def test_a_live_engine_cannot_switch_its_ledger_off(engine, provider):
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"])
    with pytest.raises(TrdError, match="cannot switch the cash ledger off"):
        engine.set_capital(None)
    # Re-basing the capital must keep the mode: upsert rewrites the whole row.
    assert engine.set_capital(D("200000")).mode == EngineMode.LIVE


# ------------------------------------------------------------------ live entry


def test_a_live_scan_writes_a_buy_intent_and_fills_nothing(engine, provider, conn):
    _names(conn, provider, "AAA")
    config, account, _ = _live(engine, ["AAA"])

    result = engine.scan(at=MONDAY)
    assert result.mode == EngineMode.LIVE
    assert result.opened == [] and result.closed == []
    assert len(result.intents) == 1
    intent = result.intents[0].intent
    assert result.intents[0].instrument.symbol == "AAA"
    assert intent.side == Side.BUY
    assert intent.status == IntentStatus.PENDING and intent.is_open
    assert intent.quantity > 0
    assert intent.notional == intent.quantity * intent.ref_price
    assert intent.strategy == "momentum"
    assert intent.signal_id is not None and intent.position_id is None
    uuid.UUID(intent.ref_id)  # a real uuid: it is the broker's idempotency key
    # The plan travels, so 1b can re-derive the stop from the real fill.
    assert intent.atr_at_entry is not None and intent.stop_price is not None
    # Stored at DECIMAL(24, 8), like engine_position.atr_at_entry, so the
    # re-derived stop agrees to the storage precision rather than to the digit.
    rederived = intent.ref_price - stop_distance(intent.atr_at_entry, config.exit_params)
    assert abs(rederived - intent.stop_price) < D("0.000001")
    assert intent.target_price is not None and intent.target_price > intent.ref_price

    assert TransactionRepo(conn).list_chronological(account.id) == []
    assert engine.position_rows(open_only=True) == []
    # Acted, exactly as a paper fill marks it.
    assert all(r.signal.acted for r in engine.signal_rows())
    assert all(s.acted for s in result.signals)

    assert [e["ev"] for e in scan_events(result)].count("intent") == 1
    assert scan_events(result)[-1]["intents"] == 1


def test_no_paper_on_a_live_engine_writes_no_intent(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    result = engine.scan(paper=False, at=MONDAY)
    assert len(result.signals) == 1
    assert result.intents == []
    assert _intents(conn, engine) == []


def test_an_open_buy_intent_holds_its_slot(engine, provider, conn):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], max_positions=1)
    first = engine.scan(at=MONDAY)
    assert len(first.intents) == 1
    assert first.capacity == 0

    again = engine.scan(at=datetime(2024, 9, 16, 11, 0))
    assert again.intents == []
    assert again.capacity == 0
    status = engine.status()
    assert status.open_intents == 1 and status.open_buy_intents == 1
    assert status.capacity == 0
    assert len(_intents(conn, engine)) == 1


def test_an_open_buy_intent_blocks_the_same_name_next_session(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"], max_positions=3)
    assert len(engine.scan(at=MONDAY).intents) == 1
    # A new session: a fresh bar, so a fresh signal — but the name is spoken for.
    provider.add_symbol("AAA", price=str(float(make_bars(uptrend())[-1].close) * 1.002))
    tuesday = engine.scan(at=datetime(2024, 9, 17, 10, 0))
    assert tuesday.intents == []
    assert len(_intents(conn, engine)) == 1


def test_an_open_buy_intent_reserves_its_cash(engine, provider, conn):
    """Settled cash must never fund two orders: the first intent's notional is
    spent from the ledger before the next scan looks at it."""
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], max_positions=3, capital=D("6000"))

    first = engine.scan(at=MONDAY)
    assert len(first.intents) == 1
    notional = first.intents[0].intent.notional
    held = first.intents[0].instrument.symbol
    other = "BBB" if held == "AAA" else "AAA"
    assert any(s.startswith(f"{other}: $") for s in first.skipped), first.skipped

    cash = engine.cash()
    assert cash is not None
    assert cash.settled == D("6000") - notional

    # A later pass: the reservation still stands, nothing new is affordable.
    later = engine.scan(at=datetime(2024, 9, 16, 11, 0))
    assert later.intents == []
    assert any(s.startswith(f"{other}: $") for s in later.skipped), later.skipped


def test_an_open_buy_intent_spends_the_entry_budget(engine, provider, conn):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], max_positions=3, max_entries_per_day=1)
    assert len(engine.scan(at=MONDAY).intents) == 1
    later = engine.scan(at=datetime(2024, 9, 16, 11, 0))
    assert later.intents == []
    assert any("entry budget spent" in s for s in later.skipped), later.skipped
    assert engine.status().entries_today == 0  # status reads the wall clock, not 2024


def test_a_paper_scan_writes_no_intents(engine, provider, conn):
    """Regression: paper behaviour is exactly what it was."""
    _names(conn, provider, "AAA")
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=D("10000"))
    result = engine.scan(at=MONDAY)
    assert result.mode == EngineMode.PAPER
    assert len(result.opened) == 1
    assert result.intents == []
    assert _intents(conn, engine) == []
    assert engine.status().open_intents == 0


# ------------------------------------------------------------------- live exit


def _filled_position(engine: EngineService, conn, symbol: str = "AAA"):
    """A live position as phase 1b will open it from a fill: a buy txn on the real
    account and an engine_position. Written directly — ingest is not built yet."""
    account = engine.account()
    instrument = InstrumentRepo(conn).get_by_symbol(symbol)
    assert instrument is not None
    price = D("200")
    TransactionRepo(conn).insert(
        account_id=account.id,
        instrument_id=instrument.id,
        side=Side.BUY,
        quantity=D("10"),
        price=price,
        fees=D(0),
        executed_at=datetime(2024, 9, 13, 10, 0),
        note="fill",
    )
    return EnginePositionRepo(conn).open(
        account_id=account.id,
        instrument_id=instrument.id,
        signal_id=None,
        strategy="momentum",
        opened_at=datetime(2024, 9, 13, 10, 0),
        entry_price=price,
        quantity=D("10"),
        stop_price=D("190"),
        target_price=D("220"),
        atr_at_entry=D("5"),
        last_bar_date=None,
    )


def test_a_live_exit_writes_one_sell_intent_and_keeps_the_position(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"], max_positions=1)
    position = _filled_position(engine, conn)
    provider.add_symbol("AAA", price="185")  # through the 190 stop

    first = engine.scan(at=MONDAY)
    assert first.closed == []
    assert len(first.intents) == 1
    sell = first.intents[0].intent
    assert sell.side == Side.SELL
    assert sell.rule == "stop"
    assert sell.position_id == position.id
    assert sell.quantity == D("10")
    assert sell.ref_price == D("185")
    assert sell.signal_id is None and sell.stop_price is None

    still = engine.position_rows(open_only=True)
    assert len(still) == 1 and still[0].position.closed_quantity == 0
    txns = TransactionRepo(conn).list_chronological(engine.account().id)
    assert [t.side for t in txns] == [Side.BUY]

    second = engine.scan(at=datetime(2024, 9, 16, 11, 0))
    assert second.intents == []
    assert len(_intents(conn, engine)) == 1
    assert len(engine.position_rows(open_only=True)) == 1
    # Still held: the slot stays occupied until the sale actually fills.
    assert second.capacity == 0
    assert engine.status().open_intents == 1


def test_a_manual_trade_on_a_live_engines_position_is_refused(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    _filled_position(engine, conn)
    portfolio = PortfolioService(conn, provider)
    with pytest.raises(EnginePositionConflictError):
        portfolio.record_trade("brokerage", "AAA", Side.SELL, D("1"), price=D("200"))


def test_trim_is_refused_on_a_live_engine(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    _filled_position(engine, conn)
    with pytest.raises(TrdError, match="trim is paper-only"):
        engine.trim("AAA", pct=D("50"), price=D("200"))
    assert engine.position_rows(open_only=True)[0].position.closed_quantity == 0


# ---------------------------------------------------------------------- backup


def test_backup_round_trips_the_mode(tmp_path: Path, engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"])
    data = export_data(conn)
    assert data["engine"]["config"]["mode"] == "live"
    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    restored = EngineService(fresh, provider).config()
    assert restored.mode == EngineMode.LIVE
    assert restored.capital == D("100000")


def test_a_backup_without_a_mode_restores_paper(tmp_path: Path, engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"])
    data = export_data(conn)
    del data["engine"]["config"]["mode"]
    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    assert EngineService(fresh, provider).config().mode == EngineMode.PAPER


# ------------------------------------------------------------------------- CLI


def test_engine_intents_json_shape(cli_env: FakeProvider):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    created = runner.invoke(
        app,
        [
            "engine",
            "init",
            "--live",
            "--account",
            "brokerage",
            "--capital",
            "5000",
            "--symbols",
            "AAA",
        ],
    )
    assert created.exit_code == 0, created.output
    assert "LIVE" in created.output

    empty = runner.invoke(app, ["engine", "intents", "--json"])
    assert empty.exit_code == 0, empty.output
    assert json.loads(empty.output) == []

    conn = connect(get_settings().db_path)
    account = AccountRepo(conn).get_by_name("brokerage")
    instrument = InstrumentRepo(conn).get_by_symbol("AAA")
    assert account is not None and instrument is not None
    OrderIntentRepo(conn).insert(
        account_id=account.id,
        instrument_id=instrument.id,
        side=Side.BUY,
        quantity=D("1.5"),
        ref_price=D("100"),
        reason="test",
        strategy="momentum",
        created_at=datetime(2026, 10, 21, 10, 0),
        stop_price=D("90"),
        target_price=D("120"),
        atr_at_entry=D("5"),
    )
    conn.close()

    out = runner.invoke(app, ["engine", "intents", "--json"])
    assert out.exit_code == 0, out.output
    rows = json.loads(out.output)
    assert len(rows) == 1
    assert set(rows[0]) == {"intent", "instrument"}
    intent = rows[0]["intent"]
    assert rows[0]["instrument"]["symbol"] == "AAA"
    assert intent["side"] == "buy"
    assert intent["status"] == "pending"
    assert intent["is_open"] is True
    assert D(intent["quantity"]) == D("1.5")
    assert D(intent["notional"]) == D("150")
    assert intent["approval_id"] is None and intent["broker_order_id"] is None

    def listed(status: str) -> list:
        out = runner.invoke(app, ["engine", "intents", "-s", status, "--json"])
        return json.loads(out.output)

    assert len(listed("pending")) == 1
    assert listed("filled") == []

    bad = runner.invoke(app, ["engine", "intents", "--status", "nope", "--json"])
    assert bad.exit_code == 1
    assert set(json.loads(bad.output)) == {"error", "message"}

    human = runner.invoke(app, ["engine", "intents"])
    assert human.exit_code == 0 and "AAA" in human.output

    status = json.loads(runner.invoke(app, ["engine", "status", "--json"]).output)
    assert status["mode"] == "live"
    assert status["open_intents"] == 1
    assert "LIVE" in runner.invoke(app, ["engine", "status"]).output

    trim = runner.invoke(app, ["engine", "trim", "AAA", "--pct", "50", "--json"])
    assert trim.exit_code == 1
    assert "paper-only" in json.loads(trim.output)["message"]
