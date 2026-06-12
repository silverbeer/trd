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
