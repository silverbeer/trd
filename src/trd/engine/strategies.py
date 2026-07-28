"""Entry strategies.

Each rule answers one question — "why would I buy this *today*?" — and refuses to
fire unless every condition lines up. All four require the trend filter (price
above the 200-day) because buying weakness in a downtrend is how paper accounts
learn expensive lessons.

The score is not a probability. It only ranks candidates that fired on the same
bar, so the engine knows which two to take when six names qualify and it has room
for two. Read the `reason`, not the number.
"""

from trd.engine.base import Strategy, StrategySignal, clamp01, indicator, last, prior, register
from trd.models import DailyBar


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
    min_bars = 200

    def evaluate(self, bars: list[DailyBar]) -> StrategySignal | None:
        price = float(bars[-1].close)
        sma50 = last(indicator("sma", bars, period=50)["value"])
        sma200 = last(indicator("sma", bars, period=200)["value"])
        rsi = last(indicator("rsi", bars, period=14)["value"])
        vol = last(indicator("volratio", bars, period=20)["ratio"])
        # `vol is None` used to fall through this filter, which meant the rule
        # silently dropped its volume requirement exactly when volume was unknown
        # — intraday, where the forming bar carries whatever the quote reports and
        # often nothing at all. Breakout already treats missing volume as
        # disqualifying; a filter that switches itself off on missing data is the
        # wrong default for a rule whose thesis is "strength that is working".
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
    min_bars = 60

    def evaluate(self, bars: list[DailyBar]) -> StrategySignal | None:
        if len(bars) < 22:
            return None
        price = float(bars[-1].close)
        window = bars[-21:-1]  # prior 20 bars, today excluded
        channel_high = max(float(b.high) for b in window)
        sma50 = last(indicator("sma", bars, period=50)["value"])
        vol = last(indicator("volratio", bars, period=20)["ratio"])
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
    min_bars = 200

    def evaluate(self, bars: list[DailyBar]) -> StrategySignal | None:
        price = float(bars[-1].close)
        sma200 = last(indicator("sma", bars, period=200)["value"])
        rsi_series = indicator("rsi", bars, period=14)["value"]
        rsi = last(rsi_series)
        rsi_prev = prior(rsi_series, 1)
        if sma200 is None or rsi is None or rsi_prev is None:
            return None
        if price <= sma200:
            return None
        recent = [v for v in rsi_series[-4:-1] if v is not None]
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
    min_bars = 200

    def evaluate(self, bars: list[DailyBar]) -> StrategySignal | None:
        price = float(bars[-1].close)
        sma200 = last(indicator("sma", bars, period=200)["value"])
        hist_series = indicator("macd", bars, fast=12, slow=26, signal=9)["hist"]
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
