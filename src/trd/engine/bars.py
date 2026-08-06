"""Where an engine's bars come from, and how a bar is stamped.

Every timeframe decision lives here. The scanner asks for a series, a forming
bar, a stamp and a held-count, and never asks which timeframe it is running —
otherwise `if config.is_intraday` grows a branch in every method it touches, and
the two paths drift.

The intraday path exists because a day engine on daily bars is inert: a stop set
at 2 x the *daily* ATR cannot be reached inside one session, so every trade exits
on the clock and the R-multiples describe a risk profile the engine never ran.
Feed the same rules 5-minute bars and the stop is sized to the move a 5-minute
bar actually makes.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trd.errors import TrdError
from trd.models import Bar, DailyBar, IntradayBar, Quote
from trd.repos import PriceRepo
from trd.timeframes import (
    DAILY,
    INTRADAY_BACKFILL_DAYS,
    INTRADAY_MINUTES,
    TIMEFRAMES,
    day_mode_on_daily_bars,
    validate_timeframe,
)

__all__ = [
    "DAILY",
    "INTRADAY_BACKFILL_DAYS",
    "INTRADAY_MINUTES",
    "TIMEFRAMES",
    "BarSource",
    "bucket_start",
    "day_mode_on_daily_bars",
    "validate_timeframe",
]


def bucket_start(moment: datetime, minutes: int) -> datetime:
    """The opening instant of the bar `moment` falls inside.

    Anchored to the hour, matching how the provider stamps its bars: 09:32 in a
    5-minute series belongs to the 09:30 bar. Anchoring to midnight instead would
    agree for every width that divides an hour and silently disagree otherwise.
    """
    floored = (moment.hour * 60 + moment.minute) // minutes * minutes
    return datetime.combine(moment.date(), time(hour=floored // 60, minute=floored % 60))


class BarSource:
    """The series an engine reasons over, at its configured timeframe."""

    def __init__(self, prices: PriceRepo | None, timeframe: str) -> None:
        self._prices = prices
        self.timeframe = validate_timeframe(timeframe)

    @classmethod
    def stamper(cls, timeframe: str) -> "BarSource":
        """A source for the bar math alone, with no database behind it — what the
        backtest needs, since it is handed its series and never loads one."""
        return cls(None, timeframe)

    @property
    def prices(self) -> PriceRepo:
        if self._prices is None:
            raise TrdError("This BarSource has no database — it can only stamp bars.")
        return self._prices

    @property
    def is_intraday(self) -> bool:
        return self.timeframe != DAILY

    @property
    def minutes(self) -> int | None:
        return INTRADAY_MINUTES.get(self.timeframe)

    @property
    def unit(self) -> str:
        """What one bar is called, for anything user-facing."""
        return "day" if self.timeframe == DAILY else self.timeframe

    def stored(self, instrument_id: int, limit: int | None = None) -> list[Bar]:
        if not self.is_intraday:
            return list(self.prices.daily_bars(instrument_id))
        return list(self.prices.intraday_bars(instrument_id, self.timeframe, limit=limit))

    def bar_counts(self) -> dict[int, int]:
        if not self.is_intraday:
            return self.prices.bar_counts()
        return self.prices.intraday_bar_counts(self.timeframe)

    def stamp(self, bar: Bar) -> datetime:
        """The instant a bar opened. A daily bar's instant is midnight."""
        if isinstance(bar, IntradayBar):
            return bar.ts
        if isinstance(bar, DailyBar):
            return datetime.combine(bar.date, time.min)
        raise TypeError(f"Not a stamped bar: {type(bar).__name__}")

    def session(self, bar: Bar) -> date:
        return self.stamp(bar).date()

    def bars_since(self, bars: list[Bar], opened_at: datetime) -> int:
        """How many completed bars the trade has lived through.

        Bar units, not calendar units — `max_bars` and `indicator_grace_bars` are
        counts of bars, so on a 5-minute engine a 10-bar time stop is 50 minutes.
        That is the honest reading: the rules measure what they can see.
        """
        if not self.is_intraday:
            return sum(1 for bar in bars if self.session(bar) > opened_at.date())
        return sum(1 for bar in bars if self.stamp(bar) > opened_at)

    def quote_is_stale(self, bars: list[Bar], quote: Quote | None, now: datetime) -> bool:
        """True when the quote carries nothing the last settled bar didn't already say.

        A symbol that has not printed yet still answers a quote request — the
        provider hands back the prior close as `last_price`. Folded in by
        `with_live_bar` that becomes a forming bar whose open, high, low and close
        are all the previous close, indistinguishable from a real flat bar. An
        entry taken on it fills at a price that never traded, and because the
        initial stop is immutable the trade keeps that fictional basis for life.

        Only applies while the current bar is still synthetic. Once a real bar for
        it exists the quote is refining known-good data, not inventing it.
        """
        if quote is None or not bars:
            return True
        if self.stamp(bars[-1]) >= self.current_bucket(now):
            return False
        return quote.price == bars[-1].close

    def with_live_bar(self, bars: list[Bar], quote: Quote | None, now: datetime) -> list[Bar]:
        """Fold the live quote into the series as the forming bar — the bar the
        current bucket will become when it settles."""
        if quote is None:
            return bars
        price = quote.price
        current = self.current_bucket(now)
        if bars and self.stamp(bars[-1]) == current:
            return [*bars[:-1], self._refine(bars[-1], price, quote)]
        return [*bars, self._open_bar(current, price, quote)]

    def current_bucket(self, now: datetime) -> datetime:
        """The instant the bar being formed right now opened."""
        minutes = self.minutes
        if minutes is None:
            return datetime.combine(now.date(), time.min)
        return bucket_start(now, minutes)

    # ------------------------------------------------------------- internals

    def _quote_volume(self, volume: int | None) -> int | None:
        """The quote's volume, but only where it is the right unit.

        A quote reports volume for the whole session — and on yfinance, extended
        hours too: MU on 2026-08-04 quoted 37,253,500 against 31,528,521 traded
        across all 79 regular-session 5-minute bars. On a *daily* forming bar that
        is exactly right, session-to-date against prior full sessions. Written
        into a *5-minute* forming bar it is roughly a hundred bars of volume in a
        one-bar slot, and `volratio` duly reported 88x-132x average, so breakout's
        volume filter — the one its docstring calls "the whole filter" — passed on
        every intraday candidate regardless of participation.

        There is no correct number to substitute. Even the true volume of a bar
        three minutes into its five would read thin against an average of complete
        bars. Volume on an intraday engine is only meaningful once a bar has
        closed, so the forming bar carries none and the rules read the last
        completed one.
        """
        return None if self.is_intraday else volume

    def _session_range(self, quote: Quote, high: Decimal, low: Decimal) -> tuple[Decimal, Decimal]:
        """Widen a forming *daily* bar to the session's real range.

        A daily sync run minutes after the open stores a bar whose high and low
        span a minute. Refining it against `last_price` alone never recovers the
        rest, so the bar keeps a sliver range all session — and ATR is computed
        from that range, so `stop = price - 2 x ATR` comes out too tight and the
        R denominator with it. Measured 2026-08-05: MSFT's stored low was 495.71
        against a 485.68 session low; its range read 0.50% where the prior two
        sessions ran 4.11% and 3.41%.

        Daily only, for the same reason the quote's volume is daily-only: a
        session high is not a 5-minute bar's high. Handing these to an intraday
        forming bar would be the identical unit error in a different column.
        """
        if self.is_intraday:
            return high, low
        if quote.day_high is not None:
            high = max(high, quote.day_high)
        if quote.day_low is not None:
            low = min(low, quote.day_low)
        return high, low

    def _refine(self, bar: Bar, price: Decimal, quote: Quote) -> Bar:
        high, low = self._session_range(quote, max(bar.high, price), min(bar.low, price))
        # A refined bar already exists in storage, so its volume is real — partial,
        # but genuinely this bucket's. Keep it rather than overwrite it.
        volume = self._quote_volume(quote.volume)
        update = {"high": high, "low": low, "close": price, "volume": volume or bar.volume}
        if isinstance(bar, IntradayBar):
            return bar.model_copy(update=update)
        assert isinstance(bar, DailyBar)
        return bar.model_copy(update=update)

    def _open_bar(self, stamp: datetime, price: Decimal, quote: Quote) -> Bar:
        if self.is_intraday:
            return IntradayBar(
                ts=stamp,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=self._quote_volume(quote.volume),
            )
        high, low = self._session_range(quote, price, price)
        return DailyBar(
            date=stamp.date(), open=price, high=high, low=low, close=price, volume=quote.volume
        )

    def backfill_window(self, latest: datetime | None, now: datetime) -> tuple[date, date]:
        """The [start, end) dates an incremental intraday fetch should ask for.

        One day of overlap on resume: the newest stored bar is usually the one
        that was still forming when it was written, so re-fetching its session is
        what settles it.
        """
        end = now.date() + timedelta(days=1)
        if latest is None:
            return end - timedelta(days=INTRADAY_BACKFILL_DAYS), end
        return latest.date(), end
