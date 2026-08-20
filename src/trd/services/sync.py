from contextlib import suppress
from datetime import date, datetime, timedelta

import duckdb
from pydantic import BaseModel

from trd.engine.bars import DAILY, BarSource
from trd.errors import ProviderError
from trd.models import Instrument, InstrumentType
from trd.providers.base import MarketDataProvider
from trd.repos import EarningsRepo, EngineConfigRepo, InstrumentRepo, PriceRepo, WatchlistRepo
from trd.services.earnings_archive import EarningsArchiveService

RECENT_DAYS = 7
FULL_BACKFILL_DAYS = 730

# How close an earnings date has to be before it is worth re-checking every pass.
# A date months out does not move in ways that matter; one inside this window is
# both the one that can still be rescheduled and the one the blackout depends on.
EARNINGS_REFRESH_HORIZON_DAYS = 10

# How many traded sessions a symbol may sit behind its peers before it is read as
# having stopped trading rather than as lagging publication. Three is deliberately
# generous: a name that misses three consecutive sessions its peers all printed is
# not late, it is halted, delisted or renamed.
DORMANT_AFTER_SESSIONS = 3


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
    # Symbols left behind by this sync — see `SyncService._freshness`. Distinct from
    # `failures`, which only ever holds symbols whose provider call *raised*.
    stale_symbols: list[str] = []
    # Behind by so many sessions they have evidently stopped trading rather than
    # merely lagged. Reported, never blocking: they can never catch up, so treating
    # them as stale would wedge `--require-current` permanently.
    dormant_symbols: list[str] = []


class SyncService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)
        self.earnings = EarningsRepo(conn)
        self.configs = EngineConfigRepo(conn)
        self.watchlists = WatchlistRepo(conn)
        self.archive = EarningsArchiveService(conn)

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
                    events = self.provider.get_earnings_dates(instrument.symbol)
                    earnings_count += self.earnings.upsert(instrument.id, events)
                    # After the bars are stored, so a release whose session just
                    # settled gets its reaction frozen on the same pass.
                    self.archive.capture(instrument, events)
                except ProviderError:
                    if instrument.symbol not in failures:
                        failures.append(instrument.symbol)
            if quote is None and instrument.symbol not in failures:
                failures.append(instrument.symbol)

        intraday_count, intraday_timeframe = self.sync_intraday(failures)
        stale, dormant = self._freshness(instruments)

        return SyncResult(
            instruments=len(instruments),
            quotes=len(quotes),
            bars=bar_count,
            earnings=earnings_count,
            failures=failures,
            intraday_bars=intraday_count,
            intraday_timeframe=intraday_timeframe,
            stale_symbols=stale,
            dormant_symbols=dormant,
        )

    def _freshness(self, instruments: list[Instrument]) -> tuple[list[str], list[str]]:
        """(stale, dormant) — symbols behind the rest of the book, split by how far.

        The failure this exists to name: `trd sync` runs once at 09:30, and yfinance
        has not published the day's daily row yet for whichever symbols the loop
        reaches first. An empty frame is a *successful* call returning zero rows, so
        it raises nothing, lands in no `failures` list, and leaves `bars` nonzero
        because the same pass re-wrote a week of gap-fill. Observed 2026-08-17: nine
        symbols kept Friday's close all session, two of them open positions, and the
        engine priced its book off a stale mark for the whole day.

        **Measured against the median, not the maximum.** The maximum is whatever
        one instrument runs furthest ahead, and something always does: at 07:30 on
        2026-08-20 `^VIX` alone carried that day's bar and all 53 stocks were
        reported stale, 90 minutes before the open. The median asks the question
        that actually matters — has this session been published *broadly* — and one
        instrument cannot move it. Note that types would not have saved this: there
        is no INDEX in `InstrumentType`, and `^VIX` is stored as a stock.

        **Behind is not the same as gone.** A symbol one session back is lagging
        publication and a retry fixes it. A symbol frozen for several sessions has
        stopped trading — halted, delisted, renamed — and can never catch up. Left
        in the same bucket it would fail `--require-current` forever, so
        `.last-sync` would never be written and the daily sync would re-run on every
        five-minute pass, all day, every day: a quiet provider-load multiplier whose
        only symptom is a slow scan. Those are reported as `dormant`, which says the
        true thing and does not block.

        Distance is counted in *sessions actually traded*, read from the dates in
        the data, so a weekend or a holiday never counts against a symbol.

        Instruments with no bars at all are excluded: a name added minutes ago has
        nothing to be behind with, and `engine status` already reports short history
        as its own condition.
        """
        latest = self.prices.latest_dates()
        known = sorted((latest[i.id], i.symbol) for i in instruments if i.id in latest)
        if not known:
            return [], []
        # Median rather than mean: dates are ordinal, and half the book being here
        # is the claim worth making.
        reference = known[len(known) // 2][0]
        rank = {day: n for n, day in enumerate(self.prices.session_dates())}

        stale: list[str] = []
        dormant: list[str] = []
        for day, symbol in known:
            if day >= reference:
                continue
            behind = rank[reference] - rank[day]
            (stale if behind <= DORMANT_AFTER_SESSIONS else dormant).append(symbol)
        return sorted(stale), sorted(dormant)

    def backfill_symbol(self, symbol: str, years: int = 2, now: datetime | None = None) -> int:
        """Pull deep history for one symbol, and only that symbol.

        `sync()` walks every tracked instrument, which is the right shape once a
        day and the wrong shape when a single name has just been added: the new
        one needs two years, everything else needs nothing, and re-pulling the
        book to serve one addition turns a chat command into a minutes-long
        provider run.

        Returns bars written (daily plus, for an intraday engine, intraday).
        Provider failures are swallowed by design — the caller reports depth from
        what actually landed, and a name that arrives empty is a name the
        strategies skip rather than a crash.
        """
        instrument = self.instruments.get_by_symbol(symbol)
        if instrument is None:
            instrument = self.instruments.insert(self.provider.get_info(symbol))

        end = date.today() + timedelta(days=1)
        start = end - timedelta(days=int(years * 365.25))
        written = 0
        try:
            bars = self.provider.get_daily_bars(instrument.symbol, start, end)
            written += self.prices.upsert_daily(instrument.id, bars)
        except ProviderError:
            return written

        if instrument.type == InstrumentType.STOCK:
            with suppress(ProviderError):
                self.earnings.upsert(
                    instrument.id, self.provider.get_earnings_dates(instrument.symbol)
                )

        # A day engine trades intraday bars, so on that engine the daily series
        # alone still leaves the name untradable.
        config = self.configs.get()
        if config is not None and config.timeframe != DAILY:
            source = BarSource(self.prices, config.timeframe)
            latest = self.prices.latest_intraday_ts(instrument.id, config.timeframe)
            window_start, window_end = source.backfill_window(latest, now or datetime.now())
            try:
                intraday = self.provider.get_intraday_bars(
                    instrument.symbol, config.timeframe, window_start, window_end
                )
                written += self.prices.upsert_intraday(instrument.id, config.timeframe, intraday)
            except ProviderError:
                pass
        return written

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
            events += self.earnings.upsert(instrument.id, dates, today)
            # This path runs on every scan, which is the one that matters for the
            # archive: it is where a newly-published date is first seen, and the
            # estimate is only point-in-time before the number lands.
            self.archive.capture(instrument, dates)
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
