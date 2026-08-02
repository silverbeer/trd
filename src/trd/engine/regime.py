"""Market regime: is the tape somewhere worth taking new risk at all?

Every entry strategy asks whether *one name* is in an uptrend — price above its
own 200-day. None of them ask what the market is doing. That is fine while
correlations are loose and actively dangerous when they are not: in a broad
selloff correlations converge toward 1, and the engine keeps filling every slot
with whatever is still above its own 200-day, straight into the decline. A live
book of AMZN / AMD / MU / SNDK / GOOGL is five slots holding one bet.

The gate is deliberately blunt and deliberately off by default. It blocks *new
entries* only — open positions keep running their exit rules, because a regime
that stops you buying is not a reason to abandon a stop that is already working.

Both switches are read from the engine's rule params. That bag is named
`exit_params` for historical reasons; it is really "the rule knobs", and it is
already threaded through both the live scanner and the backtest, which is what
lets those two share this exact code instead of growing two versions of it.
"""

from collections.abc import Sequence
from datetime import date

from trd.engine.base import indicator, last
from trd.models import Bar, DailyBar

# The tape, and the fear gauge. Both are in `trd.data.universe` already — SPY as
# an index proxy, ^VIX as the volatility read — so prep and the engine cannot
# disagree about what they mean.
TREND_SYMBOL = "SPY"
VIX_SYMBOL = "^VIX"

REGIME_SYMBOLS: tuple[str, ...] = (TREND_SYMBOL, VIX_SYMBOL)


def is_configured(params: dict[str, float]) -> bool:
    """True when any regime switch is on. Off is the default for every engine
    that existed before this, so behaviour is unchanged until someone opts in."""
    return float(params.get("regime_sma", 0)) > 0 or float(params.get("regime_vix_max", 0)) > 0


def blocks_entries(
    params: dict[str, float],
    trend_bars: Sequence[Bar] | None = None,
    vix_bars: Sequence[Bar] | None = None,
) -> str | None:
    """The reason new entries are blocked right now, or None to proceed.

    Bars must already be sliced to the moment being evaluated — the caller owns
    the as-of window. The live scanner passes everything it has; the backtest
    passes the prefix up to the bar it is standing on, which is the whole
    lookahead guarantee.

    Missing data does **not** block. A regime filter that silently halts trading
    because a sync failed would look identical to a filter that is working, and
    the engine would sit flat for days with nothing in the log to explain it. A
    gate that cannot see says so and gets out of the way; `trd engine status`
    is where a missing series should be visible.
    """
    sma_period = int(params.get("regime_sma", 0))
    if sma_period > 0 and trend_bars:
        close = float(trend_bars[-1].close)
        sma = last(indicator("sma", trend_bars, period=sma_period)["value"])
        if sma is not None and close < sma:
            return (
                f"{TREND_SYMBOL} {close:.2f} is below its {sma_period}-day ({sma:.2f}) — "
                "no new entries while the whole tape is in a downtrend"
            )

    vix_max = float(params.get("regime_vix_max", 0))
    if vix_max > 0 and vix_bars:
        vix = float(vix_bars[-1].close)
        if vix > vix_max:
            return (
                f"{VIX_SYMBOL} at {vix:.1f} is above {vix_max:.0f} — no new entries while "
                "volatility is this high, because correlations converge and five slots "
                "become one bet"
            )

    return None


def slice_to(bars: Sequence[DailyBar] | None, as_of: date) -> list[DailyBar]:
    """Bars up to and including `as_of`, for a caller walking history.

    Regime bars are always daily even when the engine trades a 5-minute
    timeframe: the question "is the market in a downtrend" is a daily one, and
    reading it off intraday bars would answer a different, noisier question.
    """
    if not bars:
        return []
    return [bar for bar in bars if bar.date <= as_of]
