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

# How close an earnings date has to be before it is worth re-checking every pass.
# A date months out does not move in ways that matter; one inside this window is
# both the one that can still be rescheduled and the one the blackout depends on.
EARNINGS_REFRESH_HORIZON_DAYS = 10


class EarningsSyncResult(BaseModel):
    """What an earnings-only refresh touched. Separate from SyncResult because it
    deliberately pulls no quotes and no bars — the whole point is that it is cheap
    enough to run on every scan."""

    checked: int
    events: int
    failures: list[str]


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

    def stale_earnings_symbols(self, today: date | None = None) -> list[str]:
        """Instruments whose earnings date is worth re-checking right now.

        Two cases, and only two — re-pulling everything every five minutes would
        spend most of its requests confirming dates months away that nobody is
        about to trade against:

        1. **No future date on record.** This is the case that caused the loss:
           the provider had not published it yet at the morning sync, so the
           blackout had nothing to protect against and the engine took the trade
           on the print. A symbol with no known date is exactly the dangerous one.
        2. **A date inside the horizon.** Companies reschedule, and a date that
           moves matters most when it is close.

        Only stocks. ETFs and crypto have no earnings and would fail every pass.
        """
        today = today or date.today()
        horizon = today + timedelta(days=EARNINGS_REFRESH_HORIZON_DAYS)
        out: list[str] = []
        for instrument in self.instruments.list_all():
            if instrument.type != InstrumentType.STOCK:
                continue
            next_date = self.earnings.next_for_instrument(instrument.id, today)
            if next_date is None or next_date <= horizon:
                out.append(instrument.symbol)
        return out

    def sync_earnings(
        self, symbols: list[str] | None = None, today: date | None = None
    ) -> EarningsSyncResult:
        """Refresh earnings dates only — no quotes, no bars.

        `trd sync` runs once a day in the engine entrypoint, which is right for
        daily bars and wrong for earnings: yfinance publishes some dates
        mid-session. Observed live on 2026-07-28 — the 09:24 sync found no date
        for BA, the engine entered it at 10:25 on a macd_cross signal, and the
        date appeared around noon. The trade was taken on the print, unprotected.

        Defaults to the symbols that are actually at risk (see
        `stale_earnings_symbols`) so this is cheap enough to run every scan.
        """
        targets = symbols if symbols is not None else self.stale_earnings_symbols(today)
        if not targets:
            return EarningsSyncResult(checked=0, events=0, failures=[])

        by_symbol = self.provider.get_earnings_dates_batch(targets)
        events = 0
        failures: list[str] = []
        for symbol in targets:
            instrument = self.instruments.get_by_symbol(symbol)
            if instrument is None:
                continue
            dates = by_symbol.get(symbol.upper())
            if dates is None:
                failures.append(symbol)
                continue
            events += self.earnings.upsert(instrument.id, dates)
        return EarningsSyncResult(checked=len(targets), events=events, failures=failures)

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
