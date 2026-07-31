"""Storage for a timeframed engine: the config field, and signals keyed to a bar
instant instead of a bar date.

Nothing reads the timeframe yet — this is the schema the intraday engine needs,
plus the guarantee that migrating to it loses nothing.
"""

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from trd.db.connection import connect
from trd.models import InstrumentInfo, SizingMode
from trd.repos import InstrumentRepo
from trd.repos.engine import EngineConfigRepo, EngineSignalRepo
from trd.services.backup import export_data, restore_data


@pytest.fixture
def signals(conn: duckdb.DuckDBPyConnection) -> EngineSignalRepo:
    return EngineSignalRepo(conn)


@pytest.fixture
def nvda(conn: duckdb.DuckDBPyConnection) -> int:
    return InstrumentRepo(conn).insert(InstrumentInfo(symbol="NVDA")).id


def _insert(signals: EngineSignalRepo, instrument_id: int, bar_ts: datetime, strategy="pullback"):
    return signals.insert(
        run_id=None,
        instrument_id=instrument_id,
        strategy=strategy,
        bar_ts=bar_ts,
        fired_at=bar_ts,
        price=Decimal("100"),
        score=0.5,
        reason="test",
    )


# ---------------------------------------------------------------- signal key


def test_one_signal_per_intraday_bucket(signals: EngineSignalRepo, nvda: int) -> None:
    """The whole reason the key widened: a 5-minute session is 78 buckets, and the
    old (symbol, strategy, date) key collapsed all of them into one row."""
    for minute in (30, 35, 40):
        _insert(signals, nvda, datetime(2026, 7, 31, 9, minute))
    stored = [signal for signal, _instrument in signals.list_recent(10)]
    assert len(stored) == 3
    assert {s.bar_ts.minute for s in stored} == {30, 35, 40}


def test_same_bucket_is_found_not_duplicated(signals: EngineSignalRepo, nvda: int) -> None:
    """A monitor loop re-derives the same signal every pass; it is stored once."""
    first = _insert(signals, nvda, datetime(2026, 7, 31, 9, 35))
    found = signals.get(nvda, "pullback", datetime(2026, 7, 31, 9, 35))
    assert found is not None
    assert found.id == first.id


def test_different_strategies_share_a_bucket(signals: EngineSignalRepo, nvda: int) -> None:
    bar = datetime(2026, 7, 31, 9, 35)
    _insert(signals, nvda, bar, strategy="pullback")
    _insert(signals, nvda, bar, strategy="breakout")
    assert len(signals.list_recent(10)) == 2


def test_daily_signal_is_a_midnight_stamp(signals: EngineSignalRepo, nvda: int) -> None:
    """A daily bar's instant is midnight — and `bar_date` still reads the session,
    so anything grouping by day is unaffected."""
    signal = _insert(signals, nvda, datetime(2026, 7, 31))
    assert signal.bar_ts == datetime(2026, 7, 31)
    assert signal.bar_date.isoformat() == "2026-07-31"


# ------------------------------------------------------------------- config


def test_timeframe_defaults_to_daily(conn: duckdb.DuckDBPyConnection) -> None:
    from trd.models import AccountType
    from trd.repos import AccountRepo

    account = AccountRepo(conn).create("engine-sim", AccountType.SIMULATION)
    config = EngineConfigRepo(conn).upsert(
        account_id=account.id,
        watchlist="engine",
        position_size=Decimal("1000"),
        max_positions=5,
        strategies=["pullback"],
        exit_params={"stop_atr_mult": 2.0},
    )
    assert config.timeframe == "1d"
    assert config.is_intraday is False


def test_timeframe_round_trips(conn: duckdb.DuckDBPyConnection) -> None:
    from trd.models import AccountType
    from trd.repos import AccountRepo

    account = AccountRepo(conn).create("engine-sim", AccountType.SIMULATION)
    repo = EngineConfigRepo(conn)
    repo.upsert(
        account_id=account.id,
        watchlist="engine",
        position_size=Decimal("10"),
        max_positions=5,
        strategies=["pullback"],
        exit_params={"flat_at_minute": 1555.0},
        timeframe="5m",
    )
    stored = repo.get()
    assert stored is not None
    assert stored.timeframe == "5m"
    assert stored.is_intraday is True


# ------------------------------------------------------------------- backup


def _engine_backup(conn: duckdb.DuckDBPyConnection, provider) -> dict:
    from tests.test_backup import _populate_engine

    _populate_engine(conn, provider)
    return export_data(conn)


def test_backup_carries_every_config_field(
    tmp_path: Path, conn: duckdb.DuckDBPyConnection, provider
) -> None:
    """A restore that drops sizing_mode or timeframe hands back an engine that
    looks like the one backed up and trades like a different one."""
    data = _engine_backup(conn, provider)
    config = data["engine"]["config"]
    assert config["timeframe"] == "1d"
    assert config["sizing_mode"] == SizingMode.EXPOSURE.value
    assert config["earnings_blackout_days"] == 3

    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    restored = EngineConfigRepo(fresh).get()
    assert restored is not None
    assert restored.timeframe == "1d"
    assert restored.sizing_mode == SizingMode.EXPOSURE
    assert restored.earnings_blackout_days == 3


def test_backup_keys_signals_by_instant(conn: duckdb.DuckDBPyConnection, provider) -> None:
    data = _engine_backup(conn, provider)
    signal = data["engine"]["signals"][0]
    assert "bar_ts" in signal
    assert "bar_date" not in signal
    assert data["engine"]["positions"][0]["signal_bar_ts"] is not None


def test_v2_backup_still_restores_and_relinks(
    tmp_path: Path, conn: duckdb.DuckDBPyConnection, provider
) -> None:
    """A backup written before this change carries a bare date. Midnight is the
    same instant, which is what migration 015 converted stored rows to — so the
    old file restores onto the new key with its position->signal link intact."""
    data = _engine_backup(conn, provider)
    data["version"] = 2
    for signal in data["engine"]["signals"]:
        signal["bar_date"] = signal.pop("bar_ts")[:10]
    for position in data["engine"]["positions"]:
        stamp = position.pop("signal_bar_ts")
        position["signal_bar_date"] = stamp[:10] if stamp else None
    # v2 files predate these three fields entirely.
    for key in ("timeframe", "sizing_mode", "earnings_blackout_days"):
        data["engine"]["config"].pop(key)

    fresh = connect(tmp_path / "v2.duckdb")
    stats = restore_data(fresh, data)
    assert stats.engine_signals == 1
    assert stats.engine_positions == 1

    stored, _instrument = EngineSignalRepo(fresh).list_recent(5)[0]
    assert stored.bar_ts.hour == 0  # a date became midnight

    linked = fresh.execute("SELECT signal_id FROM engine_position ORDER BY id LIMIT 1").fetchone()
    assert linked is not None and linked[0] is not None  # re-linked despite the shape change

    config = EngineConfigRepo(fresh).get()
    assert config is not None
    assert config.timeframe == "1d"  # the behaviour a pre-015 engine always had
    assert config.sizing_mode == SizingMode.EXPOSURE
