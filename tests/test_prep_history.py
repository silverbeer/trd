import json
from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from trd.models import DailyBar, EarningsDate
from trd.services import PrepHistoryService, SundayPrepBriefing, SundayPrepService

from .conftest import FakeProvider

SUNDAY = date(2026, 6, 14)


def _series(end: date, days: int, start_price: float, daily_gain: float) -> list[DailyBar]:
    bars: list[DailyBar] = []
    price = start_price
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        v = Decimal(str(round(price, 4)))
        bars.append(DailyBar(date=d, open=v, high=v, low=v, close=v, volume=1, adj_close=v))
        price += daily_gain
    return bars


@pytest.fixture
def briefing() -> SundayPrepBriefing:
    fake = FakeProvider()
    fake.add_symbol("ES=F", price="5000", prev_close="4950")  # +1.01%
    fake.add_symbol("^VIX", price="18.50", prev_close="18.00")
    fake.add_bars("XLK", _series(SUNDAY, 8, 100, 1.0))
    fake.add_bars("XLU", _series(SUNDAY, 8, 100, -0.5))
    for sym in ("XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLC"):
        fake.add_bars(sym, _series(SUNDAY, 8, 100, 0.1))
    fake.set_earnings("NVDA", [EarningsDate(date=date(2026, 6, 17))])
    return SundayPrepService(fake).build(SUNDAY)


def test_save_then_history_roundtrip(
    conn: duckdb.DuckDBPyConnection, briefing: SundayPrepBriefing
) -> None:
    service = PrepHistoryService(conn)
    service.save(briefing)
    rows = service.history()
    assert len(rows) == 1
    row = rows[0]
    assert row.snapshot_date == SUNDAY
    assert row.week_start == date(2026, 6, 15)
    assert row.vix == 18.5
    assert row.top_sector == "XLK"
    assert row.top_sector_pct is not None and row.top_sector_pct > 0
    assert row.worst_sector == "XLU"
    assert row.fomc_week is True  # FOMC lands Jun 17
    assert row.earnings_count == 1


def test_save_is_idempotent_per_date(
    conn: duckdb.DuckDBPyConnection, briefing: SundayPrepBriefing
) -> None:
    service = PrepHistoryService(conn)
    service.save(briefing)
    service.save(briefing)  # same date again -> overwrite, not duplicate
    assert len(service.history()) == 1


def test_payload_is_recoverable(
    conn: duckdb.DuckDBPyConnection, briefing: SundayPrepBriefing
) -> None:
    PrepHistoryService(conn).save(briefing)
    from trd.repos import PrepSnapshotRepo

    payload = PrepSnapshotRepo(conn).latest_payload()
    assert payload is not None
    restored = SundayPrepBriefing.model_validate_json(payload)
    assert restored.generated_for == briefing.generated_for
    assert len(restored.futures) == len(briefing.futures)
    # The JSON column holds the whole briefing, not just the flat columns.
    assert json.loads(payload)["tone"] == briefing.tone


def test_history_ordered_recent_first(conn: duckdb.DuckDBPyConnection) -> None:
    fake = FakeProvider()
    fake.add_symbol("^VIX", price="15.00", prev_close="15.00")
    service = PrepHistoryService(conn)
    for ref in (date(2026, 5, 31), date(2026, 6, 7), date(2026, 6, 14)):
        service.save(SundayPrepService(fake).build(ref))
    rows = service.history()
    assert [r.snapshot_date for r in rows] == [
        date(2026, 6, 14),
        date(2026, 6, 7),
        date(2026, 5, 31),
    ]
