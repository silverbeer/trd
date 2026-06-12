from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from trd.db.connection import connect
from trd.errors import ProviderError
from trd.models import (
    AccountType,
    DailyBar,
    EarningsDate,
    InstrumentInfo,
    InstrumentType,
    Quote,
)
from trd.repos import AccountRepo
from trd.services import PortfolioService, SyncService, WatchlistService


class FakeProvider:
    """In-memory MarketDataProvider. No network ever touches the test suite."""

    def __init__(self) -> None:
        self.infos: dict[str, InstrumentInfo] = {}
        self.quotes: dict[str, Quote] = {}
        self.bars: dict[str, list[DailyBar]] = {}
        self.earnings: dict[str, list[EarningsDate]] = {}

    def add_symbol(
        self,
        symbol: str,
        price: str,
        prev_close: str | None = None,
        type_: InstrumentType = InstrumentType.STOCK,
        name: str | None = None,
        year_high: str | None = None,
        year_low: str | None = None,
        volume: int | None = None,
        avg_volume: int | None = None,
    ) -> None:
        symbol = symbol.upper()
        self.infos[symbol] = InstrumentInfo(
            symbol=symbol, name=name or f"{symbol} Inc", type=type_, exchange="TEST", currency="USD"
        )
        self.quotes[symbol] = Quote(
            symbol=symbol,
            price=Decimal(price),
            prev_close=Decimal(prev_close) if prev_close else None,
            year_high=Decimal(year_high) if year_high else None,
            year_low=Decimal(year_low) if year_low else None,
            volume=volume,
            avg_volume=avg_volume,
        )

    def drop_quote(self, symbol: str) -> None:
        self.quotes.pop(symbol.upper(), None)

    def get_quote(self, symbol: str) -> Quote:
        quote = self.quotes.get(symbol.upper())
        if quote is None:
            raise ProviderError(f"No price available for {symbol}")
        return quote

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        return {s.upper(): self.quotes[s.upper()] for s in symbols if s.upper() in self.quotes}

    def get_info(self, symbol: str) -> InstrumentInfo:
        info = self.infos.get(symbol.upper())
        if info is None:
            raise ProviderError(f"Symbol {symbol} not found")
        return info

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        if symbol.upper() not in self.infos:
            raise ProviderError(f"Symbol {symbol} not found")
        return [b for b in self.bars.get(symbol.upper(), []) if start <= b.date < end]

    def get_earnings_dates(self, symbol: str) -> list[EarningsDate]:
        return self.earnings.get(symbol.upper(), [])


@pytest.fixture
def provider() -> FakeProvider:
    fake = FakeProvider()
    fake.add_symbol("AAPL", price="200.00", prev_close="195.00")
    fake.add_symbol("NVDA", price="120.00", prev_close="121.00")
    fake.add_symbol(
        "BTC-USD", price="100000.00", prev_close="98000.00", type_=InstrumentType.CRYPTO
    )
    return fake


@pytest.fixture
def conn(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    return connect(tmp_path / "test.duckdb")


@pytest.fixture
def portfolio(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> PortfolioService:
    service = PortfolioService(conn, provider)
    AccountRepo(conn).create("main", AccountType.REAL)
    return service


@pytest.fixture
def sync_service(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> SyncService:
    return SyncService(conn, provider)


@pytest.fixture
def watchlist(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> WatchlistService:
    return WatchlistService(conn, provider)


def seed_bars(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    days: int,
    start_price: float,
    daily_gain: float,
) -> None:
    """Populate price_daily with synthetic bars (close = adj_close = price)."""
    from trd.repos import InstrumentRepo, PriceRepo

    repo = InstrumentRepo(conn)
    instrument = repo.get_by_symbol(symbol) or repo.insert(
        InstrumentInfo(symbol=symbol, name=symbol, type=InstrumentType.ETF)
    )
    today = date.today()
    bars = []
    price = start_price
    for i in range(days):
        value = Decimal(str(round(price, 4)))
        bars.append(
            DailyBar(
                date=today - timedelta(days=days - i),
                open=value,
                high=Decimal(str(round(price * 1.01, 4))),
                low=Decimal(str(round(price * 0.99, 4))),
                close=value,
                volume=1_000_000,
                adj_close=value,
            )
        )
        price += daily_gain
    PriceRepo(conn).upsert_daily(instrument.id, bars)
