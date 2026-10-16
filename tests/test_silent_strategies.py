"""A configured strategy that has stopped firing is a broken engine that looks like
a quiet one (SB-1341). Pullback on the day engine produced zero signals for two
months after its RSI lookback became 1,092 five-minute bars, and nothing said so:
the scan ran, the book filled from the other three rules, and every view read as
a calm market. Status is where that has to surface.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.services import EngineService
from trd.services.engine import SILENT_STRATEGY_SESSIONS

START = datetime(2026, 9, 1, 10, 0)


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    provider.add_symbol("AAA", price="100")
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum", "pullback"])
    return service


def scan_sessions(engine: EngineService, sessions: int, scans_per_session: int = 3) -> None:
    """Record scans on consecutive days, several per day."""
    for day in range(sessions):
        for n in range(scans_per_session):
            run = engine.runs.start(START + timedelta(days=day, minutes=5 * n), paper=True)
            engine.runs.finish(run.id, scanned=1, signals=0, opened=0, closed=0)


def fire(engine: EngineService, strategy: str, at: datetime) -> None:
    instrument = engine.universe()[0]
    engine.signals.insert(None, instrument.id, strategy, at, at, Decimal("100"), 0.5, "fired")


def test_a_strategy_that_never_fires_is_flagged_once_the_sessions_add_up(engine) -> None:
    scan_sessions(engine, SILENT_STRATEGY_SESSIONS)
    fire(engine, "momentum", START + timedelta(days=SILENT_STRATEGY_SESSIONS - 1))

    assert engine.status().silent_strategies == [("pullback", SILENT_STRATEGY_SESSIONS)]


def test_silence_is_counted_in_sessions_not_scans(engine) -> None:
    """A day engine scans ~78 times a session; ten sessions of quiet must mean
    ten sessions on it and on a swing engine alike, never 'ten scans'."""
    scan_sessions(engine, SILENT_STRATEGY_SESSIONS - 1, scans_per_session=50)
    fire(engine, "momentum", START)

    assert engine.status().silent_strategies == []


def test_a_signal_passed_over_still_counts_as_the_rule_being_alive(engine) -> None:
    """macd_cross fires a handful a week and is outranked for the slots every
    time — rare and unlucky, not broken. Any signal, taken or not, clears it."""
    scan_sessions(engine, SILENT_STRATEGY_SESSIONS + 5)
    last_day = START + timedelta(days=SILENT_STRATEGY_SESSIONS + 4)
    fire(engine, "pullback", last_day)  # passed over: never acted on
    fire(engine, "momentum", last_day)

    assert engine.status().silent_strategies == []


def test_only_sessions_after_the_last_signal_count(engine) -> None:
    scan_sessions(engine, 30)
    fire(engine, "momentum", START + timedelta(days=29))
    # Last pullback signal on day 4: days 5..29 are 25 quiet sessions.
    fire(engine, "pullback", START + timedelta(days=4, hours=6))

    assert engine.status().silent_strategies == [("pullback", 25)]
