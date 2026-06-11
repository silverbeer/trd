from typing import Any

from trd.indicators import math as m
from trd.indicators.base import Category, Indicator, closes, latest, register
from trd.models import DailyBar


@register
class SMA(Indicator):
    key = "sma"
    name = "Simple Moving Average"
    category = Category.TREND
    default_params = {"period": 50}
    components = ["value"]
    description = (
        "Average close over the last N days. Price above a rising SMA = uptrend; "
        "below a falling SMA = downtrend. The 50- and 200-day are the classics."
    )

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        return {"value": m.sma(closes(bars), params["period"])}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        value = latest(series["value"])
        if value is None:
            return "not enough history yet"
        price = float(bars[-1].close)
        gap = (price - value) / value * 100
        side = "above" if gap >= 0 else "below"
        trend = "uptrend intact" if gap >= 0 else "below trend, caution"
        return f"price {side} average by {abs(gap):.1f}% — {trend}"

    def required_bars(self, **params: Any) -> int:
        return params["period"]


@register
class EMA(Indicator):
    key = "ema"
    name = "Exponential Moving Average"
    category = Category.TREND
    default_params = {"period": 21}
    components = ["value"]
    description = (
        "Like SMA but weights recent days more, so it turns faster. "
        "Short EMAs (8/21) are popular for swing-trade timing."
    )

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        return {"value": m.ema(closes(bars), params["period"])}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        value = latest(series["value"])
        if value is None:
            return "not enough history yet"
        price = float(bars[-1].close)
        gap = (price - value) / value * 100
        return f"price {'above' if gap >= 0 else 'below'} EMA by {abs(gap):.1f}%"

    def required_bars(self, **params: Any) -> int:
        return params["period"]


@register
class RSI(Indicator):
    key = "rsi"
    name = "Relative Strength Index"
    category = Category.MOMENTUM
    default_params = {"period": 14}
    components = ["value"]
    description = (
        "Momentum oscillator 0-100. Above 70 = overbought (rally stretched), "
        "below 30 = oversold (selloff stretched). Divergence from price is the pro signal."
    )

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        return {"value": m.rsi(closes(bars), params["period"])}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        value = latest(series["value"])
        if value is None:
            return "not enough history yet"
        if value >= 70:
            return f"{value:.0f} — overbought, rally stretched; chasing here is risky"
        if value <= 30:
            return f"{value:.0f} — oversold, selloff stretched; watch for a bounce"
        return f"{value:.0f} — neutral zone, no extreme"

    def required_bars(self, **params: Any) -> int:
        return params["period"] + 1


@register
class MACD(Indicator):
    key = "macd"
    name = "MACD"
    category = Category.MOMENTUM
    default_params = {"fast": 12, "slow": 26, "signal": 9}
    components = ["macd", "signal", "hist"]
    description = (
        "Gap between fast and slow EMAs, plus a signal line. Histogram above zero and "
        "growing = momentum building; crossing below the signal line = momentum fading."
    )

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        line, sig, hist = m.macd(closes(bars), params["fast"], params["slow"], params["signal"])
        return {"macd": line, "signal": sig, "hist": hist}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        hist = series["hist"]
        value = latest(hist)
        if value is None:
            return "not enough history yet"
        prev = next((h for h in reversed(hist[:-1]) if h is not None), None)
        direction = ""
        if prev is not None:
            direction = ", building" if abs(value) > abs(prev) else ", fading"
        side = "bullish" if value > 0 else "bearish"
        return f"histogram {value:+.2f} — {side} momentum{direction}"

    def required_bars(self, **params: Any) -> int:
        return params["slow"] + params["signal"]


@register
class Bollinger(Indicator):
    key = "bollinger"
    name = "Bollinger Bands"
    category = Category.VOLATILITY
    default_params = {"period": 20, "mult": 2.0}
    components = ["upper", "middle", "lower"]
    description = (
        "Bands N standard deviations around a 20-day average. Price hugging the upper band = "
        "strong but stretched; tight bands (squeeze) often precede a big move."
    )

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        upper, mid, lower = m.bollinger(closes(bars), params["period"], params["mult"])
        return {"upper": upper, "middle": mid, "lower": lower}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        upper, lower = latest(series["upper"]), latest(series["lower"])
        if upper is None or lower is None:
            return "not enough history yet"
        price = float(bars[-1].close)
        span = upper - lower
        if span == 0:
            return "bands collapsed — no signal"
        pos = (price - lower) / span * 100
        if pos >= 95:
            return f"at upper band ({pos:.0f}%) — strong but stretched"
        if pos <= 5:
            return f"at lower band ({pos:.0f}%) — weak, watch for reversal or breakdown"
        return f"{pos:.0f}% of band range — inside the bands, unremarkable"

    def required_bars(self, **params: Any) -> int:
        return params["period"]


@register
class ATR(Indicator):
    key = "atr"
    name = "Average True Range"
    category = Category.VOLATILITY
    default_params = {"period": 14}
    components = ["value"]
    description = (
        "Average daily trading range in dollars. The position-sizing input: "
        "risk per share is roughly 1-2x ATR. High ATR% = volatile name, size down."
    )

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        highs = [float(b.high) for b in bars]
        lows = [float(b.low) for b in bars]
        return {"value": m.atr(highs, lows, closes(bars), params["period"])}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        value = latest(series["value"])
        if value is None:
            return "not enough history yet"
        price = float(bars[-1].close)
        pct = value / price * 100 if price else 0
        flavor = "calm" if pct < 2 else "normal" if pct < 4 else "volatile — size positions down"
        return f"${value:.2f}/day ({pct:.1f}% of price) — {flavor}"

    def required_bars(self, **params: Any) -> int:
        return params["period"] + 1


@register
class Range52w(Indicator):
    key = "range52w"
    name = "52-Week Range Position"
    category = Category.TREND
    default_params = {}
    components = ["high", "low", "pct"]
    description = (
        "Where price sits between its 52-week low (0%) and high (100%). "
        "Leaders make new highs; bottom-quartile names are in downtrends or basing."
    )
    min_bars = 30

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        window = bars[-252:]
        high = max(float(b.high) for b in window)
        low = min(float(b.low) for b in window)
        price = float(bars[-1].close)
        pct = (price - low) / (high - low) * 100 if high != low else None
        pad: list[float | None] = [None] * (len(bars) - 1)
        return {"high": [*pad, high], "low": [*pad, low], "pct": [*pad, pct]}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        pct = latest(series["pct"])
        if pct is None:
            return "not enough history yet"
        if pct >= 90:
            return f"{pct:.0f}% — near 52-week highs, leadership behavior"
        if pct <= 20:
            return f"{pct:.0f}% — near 52-week lows, downtrend or basing"
        return f"{pct:.0f}% of 52-week range"


@register
class VolumeRatio(Indicator):
    key = "volratio"
    name = "Volume vs Average"
    category = Category.VOLUME
    default_params = {"period": 20}
    components = ["ratio"]
    description = (
        "Latest volume vs its N-day average. Moves on heavy volume (>1.5x) are "
        "trustworthy; moves on thin volume often reverse. Volume confirms price."
    )

    def compute(self, bars: list[DailyBar], **params: Any) -> dict[str, list[float | None]]:
        period = params["period"]
        vols = [float(b.volume) if b.volume else None for b in bars]
        out: list[float | None] = [None] * len(bars)
        for i in range(period, len(bars)):
            window = [v for v in vols[i - period : i] if v is not None]
            v = vols[i]
            if v is not None and window and sum(window) > 0:
                out[i] = v / (sum(window) / len(window))
        return {"ratio": out}

    def interpret(self, series: dict[str, list[float | None]], bars: list[DailyBar]) -> str:
        value = latest(series["ratio"])
        if value is None:
            return "no volume data"
        if value >= 1.5:
            return f"{value:.1f}x average — heavy volume, today's move has conviction"
        if value <= 0.5:
            return f"{value:.1f}x average — thin volume, don't trust today's move"
        return f"{value:.1f}x average — normal participation"

    def required_bars(self, **params: Any) -> int:
        return params["period"] + 1
