"""Entry strategies.

Each rule answers one question — "why would I buy this *today*?" — and refuses to
fire unless every condition lines up. All four require the trend filter (price
above the 200-day) because buying weakness in a downtrend is how paper accounts
learn expensive lessons.

The score is not a probability. It only ranks candidates that fired on the same
bar, so the engine knows which two to take when six names qualify and it has room
for two. Read the `reason`, not the number.
"""

from trd.engine.base import (
    Strategy,
    StrategyContext,
    StrategySignal,
    clamp01,
    indicator,
    last,
    last_closed,
    prior,
    register,
)

# Every lookback here is denominated in *sessions*, matching the exit rules.
# Trend constants are read off `ctx.daily` (one bar per session, so the number is
# already in bars); trigger constants go through `ctx.periods()` to become bars of
# the engine's own timeframe. Before this split they were raw bar counts, which
# read correctly on a swing engine and meant something else entirely on an
# intraday one: a 5-minute engine's "200-day trend filter" was 200 bars, or 2.6
# sessions, and every description this module prints was wrong about what the
# rule had actually checked.
TREND_FAST_SESSIONS = 50
TREND_SLOW_SESSIONS = 200
CHANNEL_SESSIONS = 20
RSI_SESSIONS = 14
VOLUME_SESSIONS = 20
MACD_SESSIONS = (12, 26, 9)
# How far back "RSI dipped recently" looks, for the pullback rule.
DIP_SESSIONS = 3


@register
class Momentum(Strategy):
    key = "momentum"
    name = "Momentum"
    description = (
        "Buy strength that is already working: price above a rising 50-day, "
        "50-day above the 200-day, and RSI in the 50-70 'strong but not stretched' "
        "band. Skips names already overbought — the goal is to join a trend, not "
        "to buy the last day of one."
    )
    trend_sessions = TREND_SLOW_SESSIONS
    signal_sessions = VOLUME_SESSIONS

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        bars = ctx.bars
        price = float(bars[-1].close)
        # Trend off daily bars; the price it is compared against is the engine's
        # own current bar, so an intraday engine still reacts intraday.
        sma50 = last(indicator("sma", ctx.daily, period=TREND_FAST_SESSIONS)["value"])
        sma200 = last(indicator("sma", ctx.daily, period=TREND_SLOW_SESSIONS)["value"])
        rsi = last(indicator("rsi", bars, period=ctx.periods(RSI_SESSIONS))["value"])
        vol = last_closed(indicator("volratio", bars, period=ctx.periods(VOLUME_SESSIONS))["ratio"])
        # `vol is None` used to fall through this filter, which meant the rule
        # silently dropped its volume requirement exactly when volume was unknown.
        # A filter that switches itself off on missing data is the wrong default
        # for a rule whose thesis is "strength that is working".
        #
        # `last_closed` rather than `last` because an intraday forming bar carries
        # no volume at all: a quote's volume covers the whole session, and a bar
        # part-way through its five minutes has no comparable number. Volume is a
        # closed-bar reading; price and RSI are not, and still use the forming bar.
        if sma50 is None or sma200 is None or rsi is None or vol is None:
            return None
        if not (price > sma50 > sma200):
            return None
        if not (50 <= rsi <= 70):
            return None
        if vol < 1.0:
            return None

        spread = clamp01((sma50 / sma200 - 1) / 0.15)
        rsi_fit = clamp01(1 - abs(rsi - 60) / 10)
        vol_bonus = clamp01((vol - 1.0) / 0.5)
        score = clamp01(0.4 * spread + 0.4 * rsi_fit + 0.2 * vol_bonus)
        gap = (price / sma50 - 1) * 100
        return StrategySignal(
            strategy=self.key,
            score=score,
            reason=(
                f"price {gap:+.1f}% vs its 50-day, 50-day sits {(sma50 / sma200 - 1) * 100:.1f}% "
                f"above the 200-day, RSI {rsi:.0f} — established uptrend with room left"
            ),
        )


@register
class Breakout(Strategy):
    key = "breakout"
    name = "Breakout"
    description = (
        "Buy a close above the highest high of the prior 20 days, but only on "
        "heavy volume (1.5x average or better) and only above the 50-day. Volume "
        "is the whole filter: a breakout nobody shows up for is a fakeout."
    )
    trend_sessions = TREND_FAST_SESSIONS
    signal_sessions = VOLUME_SESSIONS

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        bars = ctx.bars
        # The channel is a statement about sessions, so it is measured on daily
        # bars — the prior 20 *sessions'* highs, which is what "the 20-day high"
        # has always meant. `ctx.daily` already excludes the session in progress,
        # so the level cannot include the bar trying to break it.
        window = list(ctx.daily[-CHANNEL_SESSIONS:])
        if len(window) < CHANNEL_SESSIONS:
            return None
        price = float(bars[-1].close)
        channel_high = max(float(b.high) for b in window)
        sma50 = last(indicator("sma", ctx.daily, period=TREND_FAST_SESSIONS)["value"])
        vol = last_closed(indicator("volratio", bars, period=ctx.periods(VOLUME_SESSIONS))["ratio"])
        if sma50 is None or vol is None:
            return None
        if price <= channel_high:
            return None
        if vol < 1.5:
            return None
        if price <= sma50:
            return None

        extension = clamp01((price / channel_high - 1) / 0.03)
        conviction = clamp01((vol - 1.5) / 1.5)
        score = clamp01(0.3 + 0.35 * extension + 0.35 * conviction)
        return StrategySignal(
            strategy=self.key,
            score=score,
            reason=(
                f"closed {(price / channel_high - 1) * 100:+.1f}% through the 20-day high "
                f"of {channel_high:.2f} on {vol:.1f}x average volume — buyers showed up "
                "for the break"
            ),
        )


@register
class Pullback(Strategy):
    key = "pullback"
    name = "Pullback"
    description = (
        "Buy a dip inside an uptrend: price still above the 200-day, RSI dipped "
        "under 40 in the last three days, and today it is turning back up. The "
        "'turning up' part matters — a falling RSI under 40 is a downtrend, not a sale."
    )
    trend_sessions = TREND_SLOW_SESSIONS
    signal_sessions = RSI_SESSIONS + DIP_SESSIONS

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        bars = ctx.bars
        price = float(bars[-1].close)
        sma200 = last(indicator("sma", ctx.daily, period=TREND_SLOW_SESSIONS)["value"])
        rsi_series = indicator("rsi", bars, period=ctx.periods(RSI_SESSIONS))["value"]
        rsi = last(rsi_series)
        # One bar back, not one session: "turning back up" is a change of
        # direction, and direction is read at the width the engine trades on.
        # The series it is read from is already session-scaled above.
        rsi_prev = prior(rsi_series, 1)
        if sma200 is None or rsi is None or rsi_prev is None:
            return None
        if price <= sma200:
            return None
        dip = ctx.periods(DIP_SESSIONS)
        recent = [v for v in rsi_series[-(dip + 1) : -1] if v is not None]
        if not recent:
            return None
        trough = min(recent)
        if trough >= 40:
            return None
        if rsi <= rsi_prev:
            return None

        depth = clamp01((40 - trough) / 20)
        trend = clamp01((price / sma200 - 1) / 0.20)
        score = clamp01(0.3 + 0.4 * depth + 0.3 * trend)
        return StrategySignal(
            strategy=self.key,
            score=score,
            reason=(
                f"RSI bottomed at {trough:.0f} and has turned up to {rsi:.0f} while price "
                f"held {(price / sma200 - 1) * 100:.1f}% above its 200-day — dip inside "
                "an intact uptrend"
            ),
        )


@register
class MacdCross(Strategy):
    key = "macd_cross"
    name = "MACD Cross"
    description = (
        "Buy the bar where the MACD histogram flips positive — fast momentum "
        "crossing back above slow — filtered to names already above their 200-day. "
        "Catches turns earlier than a moving-average cross, at the cost of more noise."
    )
    trend_sessions = TREND_SLOW_SESSIONS
    signal_sessions = MACD_SESSIONS[1] + MACD_SESSIONS[2]

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        bars = ctx.bars
        price = float(bars[-1].close)
        sma200 = last(indicator("sma", ctx.daily, period=TREND_SLOW_SESSIONS)["value"])
        fast, slow, signal_p = (ctx.periods(n) for n in MACD_SESSIONS)
        hist_series = indicator("macd", bars, fast=fast, slow=slow, signal=signal_p)["hist"]
        hist = last(hist_series)
        hist_prev = prior(hist_series, 1)
        if sma200 is None or hist is None or hist_prev is None:
            return None
        if price <= sma200:
            return None
        if not (hist > 0 >= hist_prev):
            return None

        strength = clamp01((hist / price * 100) / 1.0)
        trend = clamp01((price / sma200 - 1) / 0.20)
        score = clamp01(0.3 + 0.4 * strength + 0.3 * trend)
        return StrategySignal(
            strategy=self.key,
            score=score,
            reason=(
                f"MACD histogram crossed up to {hist:+.2f} from {hist_prev:+.2f} with price "
                f"{(price / sma200 - 1) * 100:.1f}% above the 200-day — momentum turning "
                "inside an uptrend"
            ),
        )
