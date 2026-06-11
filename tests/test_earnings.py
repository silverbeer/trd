from datetime import date, timedelta
from decimal import Decimal

import duckdb

from trd.models import EarningsDate, Side
from trd.services import EarningsService, PortfolioService, SyncService


def test_upcoming_window_and_order(
    conn: duckdb.DuckDBPyConnection, portfolio: PortfolioService
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    portfolio.record_trade("main", "NVDA", Side.BUY, Decimal(1), Decimal(100))
    today = date.today()
    aapl = portfolio.instruments.get_by_symbol("AAPL")
    nvda = portfolio.instruments.get_by_symbol("NVDA")
    assert aapl and nvda
    service = EarningsService(conn)
    service.earnings.upsert(
        aapl.id,
        [
            EarningsDate(date=today + timedelta(days=10), eps_estimate=Decimal("2.10")),
            EarningsDate(date=today + timedelta(days=100)),  # beyond window
            EarningsDate(date=today - timedelta(days=80)),  # past
        ],
    )
    service.earnings.upsert(nvda.id, [EarningsDate(date=today + timedelta(days=3))])

    events = service.upcoming(days=14)
    assert [(e.instrument.symbol, e.date) for e in events] == [
        ("NVDA", today + timedelta(days=3)),
        ("AAPL", today + timedelta(days=10)),
    ]


def test_sync_pulls_earnings_for_stocks_only(
    portfolio: PortfolioService, sync_service: SyncService, provider
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    portfolio.record_trade("main", "BTC-USD", Side.BUY, Decimal("0.1"), Decimal(90000))
    today = date.today()
    provider.earnings["AAPL"] = [EarningsDate(date=today + timedelta(days=7))]
    provider.earnings["BTC-USD"] = [EarningsDate(date=today + timedelta(days=7))]  # must be ignored

    result = sync_service.sync()
    assert result.earnings == 1
    events = EarningsService(sync_service.conn).upcoming(days=14)
    assert [e.instrument.symbol for e in events] == ["AAPL"]


def test_upsert_idempotent(conn: duckdb.DuckDBPyConnection, portfolio: PortfolioService) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    aapl = portfolio.instruments.get_by_symbol("AAPL")
    assert aapl
    service = EarningsService(conn)
    when = date.today() + timedelta(days=5)
    service.earnings.upsert(aapl.id, [EarningsDate(date=when)])
    service.earnings.upsert(aapl.id, [EarningsDate(date=when, eps_estimate=Decimal("1.5"))])
    events = service.upcoming(days=14)
    assert len(events) == 1
    assert events[0].eps_estimate == Decimal("1.5")
