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

DAILY = "1d"

# Bar width in minutes. The keys are the timeframes an engine may be configured
# with; anything else is refused at init rather than discovered at scan time.
INTRADAY_MINUTES: dict[str, int] = {"5m": 5, "15m": 15, "30m": 30, "1h": 60}

TIMEFRAMES: tuple[str, ...] = (DAILY, *INTRADAY_MINUTES)

# How much intraday history a first sync asks for. The provider caps this anyway
# (yfinance serves ~60 days of 5-minute bars); asking for more is not an error,
# it just returns what exists.
INTRADAY_BACKFILL_DAYS = 59


def validate_timeframe(timeframe: str) -> str:
    if timeframe not in TIMEFRAMES:
        raise TrdError(
            f"Unknown timeframe {timeframe!r}. Available: {', '.join(TIMEFRAMES)}. "
            f"'{DAILY}' is a swing engine; the rest read intraday bars."
        )
    return timeframe


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

    def __init__(self, prices: PriceRepo, timeframe: str) -> None:
        self.prices = prices
        self.timeframe = validate_timeframe(timeframe)

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
            return [*bars[:-1], self._refine(bars[-1], price, quote.volume)]
        return [*bars, self._open_bar(current, price, quote.volume)]

    def current_bucket(self, now: datetime) -> datetime:
        """The instant the bar being formed right now opened."""
        minutes = self.minutes
        if minutes is None:
            return datetime.combine(now.date(), time.min)
        return bucket_start(now, minutes)

    # ------------------------------------------------------------- internals

    def _refine(self, bar: Bar, price: Decimal, volume: int | None) -> Bar:
        high = max(bar.high, price)
        low = min(bar.low, price)
        if isinstance(bar, IntradayBar):
            return bar.model_copy(
                update={"high": high, "low": low, "close": price, "volume": volume or bar.volume}
            )
        assert isinstance(bar, DailyBar)
        return bar.model_copy(
            update={"high": high, "low": low, "close": price, "volume": volume or bar.volume}
        )

    def _open_bar(self, stamp: datetime, price: Decimal, volume: int | None) -> Bar:
        if self.is_intraday:
            return IntradayBar(
                ts=stamp, open=price, high=price, low=price, close=price, volume=volume
            )
        return DailyBar(
            date=stamp.date(), open=price, high=price, low=price, close=price, volume=volume
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
