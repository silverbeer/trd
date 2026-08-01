"""Earnings-only sync.

The blackout can only protect a name whose earnings date is already stored, and
yfinance publishes some dates mid-session. `trd sync` runs once a day in both
runners, which is right for bars and wrong for this — so earnings get their own
cheap refresh that runs every scan.

The test that matters is `test_the_ba_scenario_is_now_caught`.
"""

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.models import EarningsDate, InstrumentInfo, InstrumentType
from trd.repos import EarningsRepo, InstrumentRepo
from trd.services import SyncService

TODAY = date(2026, 7, 28)


@pytest.fixture
def sync(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> SyncService:
    return SyncService(conn, provider)


def _add(conn: duckdb.DuckDBPyConnection, symbol: str, type_=InstrumentType.STOCK) -> int:
    return InstrumentRepo(conn).insert(InstrumentInfo(symbol=symbol, type=type_)).id


# ------------------------------------------------------- who is worth checking


def test_a_symbol_with_no_known_date_is_stale(sync, conn) -> None:
    """The case that caused the loss: nothing on record means nothing to protect
    against, so this is the symbol most worth re-checking."""
    _add(conn, "BA")
    assert sync.stale_earnings_symbols(TODAY) == ["BA"]


def test_a_date_far_out_is_not_worth_rechecking(sync, conn) -> None:
    """Re-pulling a date months away every five minutes spends requests confirming
    something nobody is about to trade against."""
    instrument_id = _add(conn, "BA")
    EarningsRepo(conn).upsert(instrument_id, [EarningsDate(date=date(2026, 10, 28))])
    assert sync.stale_earnings_symbols(TODAY) == []


def test_a_date_inside_the_horizon_is_rechecked(sync, conn) -> None:
    """Companies reschedule, and it matters most when the date is close."""
    instrument_id = _add(conn, "BA")
    EarningsRepo(conn).upsert(instrument_id, [EarningsDate(date=TODAY + timedelta(days=3))])
    assert sync.stale_earnings_symbols(TODAY) == ["BA"]


def test_a_past_date_does_not_count_as_known(sync, conn) -> None:
    instrument_id = _add(conn, "BA")
    EarningsRepo(conn).upsert(instrument_id, [EarningsDate(date=date(2026, 4, 22))])
    assert sync.stale_earnings_symbols(TODAY) == ["BA"]


def test_non_stocks_are_skipped(sync, conn) -> None:
    """ETFs and crypto have no earnings; including them would fail every pass."""
    _add(conn, "SPY", InstrumentType.ETF)
    _add(conn, "BTC-USD", InstrumentType.CRYPTO)
    assert sync.stale_earnings_symbols(TODAY) == []


# --------------------------------------------------------------- the refresh


def test_the_ba_scenario_is_now_caught(sync, conn, provider) -> None:
    """Reproduces 2026-07-28 exactly.

    At 09:24 the provider knows nothing about BA's earnings, so a full sync stores
    nothing and the blackout has no date to hold the engine back. By noon the
    provider has published it. Before this change the next check was the following
    morning, and the engine took the trade on the print for -20.04.
    """
    instrument_id = _add(conn, "BA")

    # 09:24 — nothing published yet.
    assert sync.sync_earnings(today=TODAY).events == 0
    assert EarningsRepo(conn).next_for_instrument(instrument_id, TODAY) is None

    # ~12:00 — the provider now has it, and it is today's print.
    provider.set_earnings("BA", [EarningsDate(date=TODAY)])
    result = sync.sync_earnings(today=TODAY)

    assert result.events == 1
    assert EarningsRepo(conn).next_for_instrument(instrument_id, TODAY) == TODAY


def test_refresh_pulls_no_quotes_or_bars(sync, conn, provider) -> None:
    """The whole point is being cheap enough to run every scan."""
    instrument_id = _add(conn, "BA")
    provider.set_earnings("BA", [EarningsDate(date=TODAY)])
    before = conn.execute("SELECT count(*) FROM price_daily").fetchone()
    snapshots_before = conn.execute("SELECT count(*) FROM quote_snapshot").fetchone()

    sync.sync_earnings(today=TODAY)

    assert conn.execute("SELECT count(*) FROM price_daily").fetchone() == before
    assert conn.execute("SELECT count(*) FROM quote_snapshot").fetchone() == snapshots_before
    assert EarningsRepo(conn).next_for_instrument(instrument_id, TODAY) == TODAY


def test_an_explicit_symbol_list_overrides_the_filter(sync, conn, provider) -> None:
    instrument_id = _add(conn, "BA")
    EarningsRepo(conn).upsert(instrument_id, [EarningsDate(date=date(2026, 10, 28))])
    provider.set_earnings("BA", [EarningsDate(date=date(2026, 10, 30))])

    assert sync.sync_earnings(today=TODAY).checked == 0  # filtered out
    assert sync.sync_earnings(symbols=["BA"], today=TODAY).checked == 1


def test_one_broken_symbol_does_not_stop_the_rest(sync, conn, provider) -> None:
    """A blackout refreshed for four names beats one refreshed for none."""
    _add(conn, "BA")
    _add(conn, "NVDA")
    provider.broken_earnings.add("BA")
    provider.set_earnings("NVDA", [EarningsDate(date=TODAY + timedelta(days=2))])

    result = sync.sync_earnings(today=TODAY)

    assert result.failures == ["BA"]
    assert result.events == 1


def test_nothing_stale_does_no_work(sync, conn) -> None:
    _add(conn, "SPY", InstrumentType.ETF)
    result = sync.sync_earnings(today=TODAY)
    assert (result.checked, result.events, result.failures) == (0, 0, [])


def test_batch_provider_omits_failures_not_raises() -> None:
    fake = FakeProvider()
    fake.add_symbol("BA", price="200")
    fake.add_symbol("NVDA", price="100")
    fake.set_earnings("NVDA", [EarningsDate(date=TODAY)])
    fake.broken_earnings.add("BA")

    got = fake.get_earnings_dates_batch(["BA", "NVDA"])
    assert "BA" not in got
    assert got["NVDA"][0].date == TODAY


# ------------------------------------------------------ blackout end to end


def _engine_with_signal(conn, provider):
    """An engine whose single name has a live momentum signal at 10:25."""
    from datetime import datetime

    from tests.test_engine import make_bars, seed, uptrend
    from trd.services import EngineService

    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    scan_at = datetime.combine(bars[-1].date, datetime.min.time()).replace(hour=10, minute=25)
    return engine, scan_at, bars[-1].date


def test_without_a_stored_date_the_trade_is_taken(conn, provider) -> None:
    """What happened to BA: no date on record, so nothing held the engine back."""
    engine, scan_at, _bar_date = _engine_with_signal(conn, provider)
    assert len(engine.scan(at=scan_at).opened) == 1


def test_a_mid_session_refresh_makes_the_blackout_hold(conn, provider) -> None:
    """The fix, end to end. Same setup, but the earnings date lands *before* the
    scan — which is exactly what running `sync --earnings-only` every pass buys.

    Deliberately a separate test rather than a second scan in the one above: once
    a signal has been acted on for a bar the scanner skips it, so a re-scan would
    report no entry for a reason that has nothing to do with earnings.
    """
    engine, scan_at, bar_date = _engine_with_signal(conn, provider)
    provider.set_earnings("AAA", [EarningsDate(date=bar_date)])

    assert SyncService(conn, provider).sync_earnings(today=bar_date).events == 1

    result = engine.scan(at=scan_at)

    assert result.opened == []
    assert any("earnings" in line for line in result.skipped)
