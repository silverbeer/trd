from datetime import date, timedelta
from decimal import Decimal

from trd.models import DailyBar, Side
from trd.services import PortfolioService, SyncService


def _bars(days: int) -> list[DailyBar]:
    today = date.today()
    return [
        DailyBar(
            date=today - timedelta(days=i),
            open=Decimal(100),
            high=Decimal(105),
            low=Decimal(99),
            close=Decimal(102),
            volume=1_000_000,
        )
        for i in range(days)
    ]


def test_sync_stores_quotes_and_bars(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    provider.bars["AAPL"] = _bars(30)

    result = sync_service.sync()
    assert result.instruments == 1
    assert result.quotes == 1
    assert 0 < result.bars <= 8  # default window: last week only
    assert result.failures == []

    instrument = portfolio.instruments.get_by_symbol("AAPL")
    assert instrument is not None
    snapshot = portfolio.prices.latest_snapshot(instrument.id)
    assert snapshot is not None
    assert snapshot[0] == Decimal("200.00")


def test_sync_full_backfills(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    provider.bars["AAPL"] = _bars(30)
    result = sync_service.sync(full=True)
    assert result.bars == 30


def test_sync_reports_failures(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    provider.drop_quote("AAPL")
    result = sync_service.sync()
    assert result.quotes == 0
    assert result.failures == ["AAPL"]


def test_sync_upsert_idempotent(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    provider.bars["AAPL"] = _bars(5)
    sync_service.sync()
    sync_service.sync()  # re-sync must not duplicate rows
    row = sync_service.conn.execute("SELECT count(*) FROM price_daily").fetchone()
    assert row is not None and row[0] == 5


def test_sync_years_widens_window(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    provider.bars["AAPL"] = _bars(1500)  # ~4 years of daily bars
    full = sync_service.sync(full=True)
    assert full.bars <= 731
    deep = sync_service.sync(years=4)
    assert deep.bars > 1400


def _bars_ending(days: int, last: date) -> list[DailyBar]:
    """`days` consecutive daily bars, the newest one dated `last`."""
    return [
        DailyBar(
            date=last - timedelta(days=i),
            open=Decimal(100),
            high=Decimal(105),
            low=Decimal(99),
            close=Decimal(102),
            volume=1_000_000,
        )
        for i in range(days)
    ]


def test_a_symbol_the_provider_answered_empty_is_reported_stale(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    """The 09:30 race: yfinance has today's bar for one name and not yet the other.

    An empty frame raises nothing and writes nothing, so before this the loser
    kept yesterday's close all session with no failure recorded anywhere.
    """
    today = date.today()
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    portfolio.record_trade("main", "NVDA", Side.BUY, Decimal(1), Decimal(100))
    provider.bars["AAPL"] = _bars_ending(5, today - timedelta(days=1))  # lost the race
    provider.bars["NVDA"] = _bars_ending(5, today)

    result = sync_service.sync()

    assert result.stale_symbols == ["AAPL"]
    assert result.failures == []  # nothing raised — that is the whole point


def test_nobody_is_stale_when_no_symbol_has_a_newer_bar(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    """A holiday, a weekend, or before the open: the whole book stops at the same
    date. Compared against the sync's own high-water mark, that is not staleness,
    and reporting it would cry wolf on every day the market never traded."""
    yesterday = date.today() - timedelta(days=1)
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    portfolio.record_trade("main", "NVDA", Side.BUY, Decimal(1), Decimal(100))
    provider.bars["AAPL"] = _bars_ending(5, yesterday)
    provider.bars["NVDA"] = _bars_ending(5, yesterday)

    assert sync_service.sync().stale_symbols == []


def test_a_symbol_with_no_bars_at_all_is_not_stale(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    """A name added minutes ago has nothing to be behind with. Short history is
    its own condition, already reported by `engine status`."""
    today = date.today()
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    portfolio.record_trade("main", "NVDA", Side.BUY, Decimal(1), Decimal(100))
    provider.bars["AAPL"] = _bars_ending(5, today)
    provider.bars["NVDA"] = []

    assert sync_service.sync().stale_symbols == []
