from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel

from trd.indicators import REGISTRY as INDICATORS
from trd.models import Bar
from trd.timeframes import DAILY, sessions_to_bars


@dataclass(frozen=True)
class StrategyContext:
    """What an entry rule is allowed to look at.

    Two series, deliberately.

    `bars` is the engine's own timeframe. The *trigger* lives here — reacting
    inside the bar is the whole point of an intraday engine, so a breakout, an
    RSI turn or a MACD cross is read at the width the engine runs on.

    `daily` is settled daily bars. The *trend* lives here, and it has to, because
    a 200-session filter is a statement about months: 200 sessions of 5-minute
    bars is 15,600 of them, and the provider serves about 4,600. Resolving the
    trend filter against the engine's own bars would either make it uncomputable
    or — the bug this fixes — quietly reduce "above the 200-day" to 2.6 sessions.
    Reading it off daily bars keeps the phrase meaning exactly what it says on
    every timeframe. The regime gate already reasons this way, gating an intraday
    engine on SPY's *daily* trend.

    `daily` never includes the session in progress. Live, that bar is still
    forming; in a backtest it is the bar the signal is about. Excluding it is
    what makes a trend filter a statement about the past rather than about the
    number it is being compared to, and it is what breakout's channel already
    did by hand ("today excluded").

    On a swing engine the two series are the same bars — `daily` is simply
    `bars` without its last entry — so every scaling below is the identity and a
    1d engine reads exactly as it always has.
    """

    bars: Sequence[Bar]
    daily: Sequence[Bar] = field(default_factory=list)
    timeframe: str = DAILY

    def periods(self, sessions: float) -> int:
        """A lookback in sessions, resolved to bars of the engine's timeframe.

        For indicators run over `bars`. Anything run over `daily` takes the
        session count unchanged — a daily bar is one session by definition.
        """
        return max(1, sessions_to_bars(self.timeframe, sessions))


class StrategySignal(BaseModel):
    """What an entry rule emits when its conditions line up.

    `reason` is the point of the whole thing — a plain-English read of *why* the
    rule fired, in the same voice as the indicator panel. A signal you can't
    explain is a signal you can't learn from.
    """

    strategy: str
    score: float  # 0..1 confidence, used only to rank same-bar candidates
    reason: str


class Strategy(ABC):
    """One entry rule in the code registry. Mirrors the Indicator contract:
    the math lives in evaluate(), the teaching read rides along in the signal.

    Both warmup figures are denominated in **sessions**, never bars — the same
    unit the exit rules were moved to. A rule tuned on "200 days" means 200
    sessions on every timeframe, and `StrategyContext` is what resolves that to
    the right number of bars for each of its two series.
    """

    key: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    # Daily-bar history the trend filter needs, in sessions.
    trend_sessions: ClassVar[int] = 200
    # Engine-timeframe history the trigger needs, in sessions.
    signal_sessions: ClassVar[int] = 20

    def warmup_bars(self, timeframe: str = DAILY) -> int:
        """Bars of the engine's timeframe needed before the rule can fire.

        On a swing engine the trend series *is* this series, so the requirement
        is whichever lookback is longer. On an intraday engine the two are
        supplied separately and only the trigger has to fit in these bars.
        """
        sessions = (
            max(self.trend_sessions, self.signal_sessions)
            if timeframe == DAILY
            else self.signal_sessions
        )
        # +1 because the current bar is the one being judged, not warmup for it.
        return sessions_to_bars(timeframe, sessions) + 1

    def warmup_daily(self, timeframe: str = DAILY) -> int:
        """Settled daily bars the trend filter needs. Zero on a swing engine,
        where `warmup_bars` already covers it and there is no second series."""
        return 0 if timeframe == DAILY else self.trend_sessions + 1

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        """Return a signal if the rule fires on the last bar, else None."""


REGISTRY: dict[str, Strategy] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    REGISTRY[cls.key] = cls()
    return cls


def indicator(key: str, bars: Sequence[Bar], **params: Any) -> dict[str, list[float | None]]:
    """Run a registered indicator over bars. Strategies never reimplement math
    that the indicator registry already owns."""
    return INDICATORS[key].compute(bars, **params)


def last(series: list[float | None]) -> float | None:
    return series[-1] if series else None


def last_closed(series: list[float | None]) -> float | None:
    """The last reading, looking back exactly one bar past a trailing gap.

    For readings that only mean something on a *completed* bar. Volume is the
    case that forced this: an intraday forming bar carries no volume, because a
    quote's volume covers the whole session and a bar part-way through its five
    minutes has no comparable number to offer. `last()` returns None there, and a
    rule that requires volume would stop firing altogether — which would remove
    momentum and breakout from every intraday engine.

    The lookback is **one bar, not any distance**. Exactly one trailing gap is the
    forming bar and expected; two in a row is the provider failing to report
    volume, and a rule whose thesis is "strength that is working" must not run on
    a number old enough to be about a different move. So a run of gaps still
    disqualifies, which is the protection the earlier fix was reaching for.

    Deliberately not the default for `last()`: price, RSI and the moving averages
    are *supposed* to reflect the forming bar — reacting inside the bar is the
    whole point of an intraday engine.
    """
    if not series:
        return None
    if series[-1] is not None:
        return series[-1]
    return series[-2] if len(series) >= 2 else None


def prior(series: list[float | None], back: int = 1) -> float | None:
    """Value `back` bars before the end, or None if it isn't there yet."""
    index = len(series) - 1 - back
    return series[index] if index >= 0 else None


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))
