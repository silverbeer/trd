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
    IntradayBar,
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
        self.intraday: dict[tuple[str, str], list[IntradayBar]] = {}
        self.earnings: dict[str, list[EarningsDate]] = {}
        self.broken_earnings: set[str] = set()

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

    def add_bars(self, symbol: str, bars: list[DailyBar]) -> None:
        """Register a symbol (info only) plus a daily-bar series for it."""
        symbol = symbol.upper()
        self.infos.setdefault(
            symbol,
            InstrumentInfo(symbol=symbol, name=symbol, type=InstrumentType.ETF, currency="USD"),
        )
        self.bars[symbol] = bars

    def add_intraday(self, symbol: str, interval: str, bars: list[IntradayBar]) -> None:
        """Register a symbol (info only) plus an intraday series at one interval."""
        symbol = symbol.upper()
        self.infos.setdefault(
            symbol,
            InstrumentInfo(symbol=symbol, name=symbol, type=InstrumentType.STOCK, currency="USD"),
        )
        self.intraday[(symbol, interval)] = bars

    def set_earnings(self, symbol: str, dates: list[EarningsDate]) -> None:
        self.earnings[symbol.upper()] = dates

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

    def get_intraday_bars(
        self, symbol: str, interval: str, start: date, end: date
    ) -> list[IntradayBar]:
        if symbol.upper() not in self.infos:
            raise ProviderError(f"Symbol {symbol} not found")
        series = self.intraday.get((symbol.upper(), interval), [])
        return [b for b in series if start <= b.ts.date() < end]

    def get_earnings_dates(self, symbol: str) -> list[EarningsDate]:
        if symbol.upper() in self.broken_earnings:
            raise ProviderError(f"Earnings fetch failed for {symbol}")
        return self.earnings.get(symbol.upper(), [])

    def get_earnings_dates_batch(self, symbols: Sequence[str]) -> dict[str, list[EarningsDate]]:
        out: dict[str, list[EarningsDate]] = {}
        for symbol in symbols:
            try:
                out[symbol.upper()] = self.get_earnings_dates(symbol)
            except ProviderError:
                continue
        return out


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
def cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: FakeProvider
) -> FakeProvider:
    """A CLI invocation pointed at a throwaway TRD_HOME with the fake provider.

    Lives here rather than in one test module so any test that drives the app
    through CliRunner gets the same isolation — an escaped TRD_HOME writes to the
    real database, and an escaped provider hits the network.
    """
    import trd.cli.app as cli

    monkeypatch.setenv("TRD_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "YFinanceProvider", lambda: provider)
    return provider


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


def months_ago(n: int, day: int = 15, today: date | None = None) -> date:
    """A date exactly `n` calendar months back, on `day` of that month.

    Not `today - timedelta(days=30 * n)`: 30-day steps are not months. From
    2026-07-30 the 3-, 2- and 1-month offsets land on 05-01, 05-31 and 06-30 —
    two of them in May. A DCA plan refuses a second contribution in a month it
    has already invested in, so tests built on 30-day arithmetic pass or fail
    depending on today's date, and fail first in UTC CI where the date rolls
    over hours before it does locally.

    Mid-month by default so no offset can drift into a neighbouring month.
    """
    today = today or date.today()
    index = today.year * 12 + (today.month - 1) - n
    return date(index // 12, index % 12 + 1, day)


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
