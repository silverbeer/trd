from datetime import date, datetime, timedelta

import duckdb
from pydantic import BaseModel

from trd.engine.bars import DAILY, BarSource
from trd.errors import ProviderError
from trd.models import InstrumentType
from trd.providers.base import MarketDataProvider
from trd.repos import EarningsRepo, EngineConfigRepo, InstrumentRepo, PriceRepo, WatchlistRepo

RECENT_DAYS = 7
FULL_BACKFILL_DAYS = 730


class SyncResult(BaseModel):
    instruments: int
    quotes: int
    bars: int
    earnings: int
    failures: list[str]
    intraday_bars: int = 0
    intraday_timeframe: str | None = None


class SyncService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)
        self.earnings = EarningsRepo(conn)
        self.configs = EngineConfigRepo(conn)
        self.watchlists = WatchlistRepo(conn)

    def sync(self, full: bool = False, years: int | None = None) -> SyncResult:
        """Refresh quotes + daily bars for every tracked instrument.

        Default pulls the last week of bars (gap-fill); --full backfills two
        years; years=N widens the backfill to N years (implies full) — needed
        for DCA forecasting/backtesting over long windows.
        """
        instruments = self.instruments.list_all()
        symbols = [i.symbol for i in instruments]
        quotes = self.provider.get_quotes(symbols)

        bar_count = 0
        earnings_count = 0
        failures: list[str] = []
        end = date.today() + timedelta(days=1)
        if years is not None:
            start = end - timedelta(days=int(years * 365.25))
        else:
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

        intraday_count, intraday_timeframe = self.sync_intraday(failures)

        return SyncResult(
            instruments=len(instruments),
            quotes=len(quotes),
            bars=bar_count,
            earnings=earnings_count,
            failures=failures,
            intraday_bars=intraday_count,
            intraday_timeframe=intraday_timeframe,
        )

    def sync_intraday(
        self, failures: list[str] | None = None, now: datetime | None = None
    ) -> tuple[int, str | None]:
        """Refresh intraday bars for an intraday engine's universe.

        Driven by the engine config rather than a flag, because the bars are not
        optional for the engine that needs them: a day engine with no intraday
        series takes no trades at all, and a flag makes that a thing you can
        forget. A swing engine, or no engine, does no extra work here.

        Incremental — only the sessions since the newest stored bar, plus that
        session again, because the newest stored bar is usually the one that was
        still forming when it was written.
        """
        failures = failures if failures is not None else []
        config = self.configs.get()
        if config is None or config.timeframe == DAILY:
            return 0, None
        board = self.watchlists.get_by_name(config.watchlist)
        if board is None:
            return 0, config.timeframe

        source = BarSource(self.prices, config.timeframe)
        count = 0
        for _list_name, instrument in self.watchlists.items(board.id):
            latest = self.prices.latest_intraday_ts(instrument.id, config.timeframe)
            start, end = source.backfill_window(latest, now or datetime.now())
            try:
                bars = self.provider.get_intraday_bars(
                    instrument.symbol, config.timeframe, start, end
                )
                count += self.prices.upsert_intraday(instrument.id, config.timeframe, bars)
            except ProviderError:
                if instrument.symbol not in failures:
                    failures.append(instrument.symbol)
        return count, config.timeframe
