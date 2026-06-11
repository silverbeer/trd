from datetime import date, timedelta

import duckdb
from pydantic import BaseModel

from trd.errors import ProviderError
from trd.models import InstrumentType
from trd.providers.base import MarketDataProvider
from trd.repos import EarningsRepo, InstrumentRepo, PriceRepo

RECENT_DAYS = 7
FULL_BACKFILL_DAYS = 730


class SyncResult(BaseModel):
    instruments: int
    quotes: int
    bars: int
    earnings: int
    failures: list[str]


class SyncService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)
        self.earnings = EarningsRepo(conn)

    def sync(self, full: bool = False) -> SyncResult:
        """Refresh quotes + daily bars for every tracked instrument.

        Default pulls the last week of bars (gap-fill); --full backfills two years.
        """
        instruments = self.instruments.list_all()
        symbols = [i.symbol for i in instruments]
        quotes = self.provider.get_quotes(symbols)

        bar_count = 0
        earnings_count = 0
        failures: list[str] = []
        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=FULL_BACKFILL_DAYS if full else RECENT_DAYS)

        for instrument in instruments:
            quote = quotes.get(instrument.symbol)
            if quote is not None:
                self.prices.insert_snapshot(instrument.id, quote.price, quote.prev_close)
            try:
                bars = self.provider.get_daily_bars(instrument.symbol, start, end)
                bar_count += self.prices.upsert_daily(instrument.id, bars)
            except ProviderError:
                failures.append(instrument.symbol)
            if instrument.type == InstrumentType.STOCK:
                try:
                    earnings_count += self.earnings.upsert(
                        instrument.id, self.provider.get_earnings_dates(instrument.symbol)
                    )
                except ProviderError:
                    if instrument.symbol not in failures:
                        failures.append(instrument.symbol)
            if quote is None and instrument.symbol not in failures:
                failures.append(instrument.symbol)

        return SyncResult(
            instruments=len(instruments),
            quotes=len(quotes),
            bars=bar_count,
            earnings=earnings_count,
            failures=failures,
        )
