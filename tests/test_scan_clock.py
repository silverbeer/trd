"""A scan fixture must not depend on the wall clock (SB-1825).

On 2026-11-27, a 13:00 half-day, about 60 tests errored after 13:00 ET and passed
again after midnight. The engine was right: past a half-day's close a scan takes
no entries (SB-1775). The fixtures were wrong, because they ran `scan()` at
`datetime.now()` and expected it to open a position. They now pass
`at=mid_session()`. These tests freeze the clock on that afternoon to show the
pinned fixture still opens and the unpinned one would not.
"""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import duckdb
import pytest
import time_machine

from tests.conftest import FakeProvider, mid_session
from tests.test_engine import make_bars, seed, uptrend
from trd.services import EngineService

HALF_DAY_AFTERNOON = datetime(2026, 11, 27, 14, 0, tzinfo=ZoneInfo("America/New_York"))


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    return service


@time_machine.travel(HALF_DAY_AFTERNOON, tick=False)
def test_a_pinned_scan_fixture_opens_on_a_half_day_afternoon(engine):
    assert mid_session() == datetime(2026, 11, 27, 10, 0)
    assert len(engine.scan(at=mid_session()).opened) == 1


@time_machine.travel(HALF_DAY_AFTERNOON, tick=False)
def test_the_same_scan_on_the_wall_clock_takes_nothing(engine):
    # The frozen clock really is past the bell: this is the failure the pin avoids.
    result = engine.scan()
    assert result.opened == []
    assert any("after the 13:00 close" in reason for reason in result.skipped)
