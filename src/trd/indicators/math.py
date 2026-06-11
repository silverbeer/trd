"""Pure indicator math over float series. None marks positions without a value yet
(warm-up window). Floats, not Decimal: indicators are signals, not ledger entries."""


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    prev = sum(values[:period]) / period  # seed with SMA
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period

    def value(g: float, l: float) -> float:  # noqa: E741
        if l == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """(macd line, signal line, histogram)."""
    ema_fast, ema_slow = ema(closes, fast), ema(closes, slow)
    line: list[float | None] = [
        f - s if f is not None and s is not None else None
        for f, s in zip(ema_fast, ema_slow, strict=True)
    ]
    valid = [v for v in line if v is not None]
    sig_valid = ema(valid, signal)
    sig: list[float | None] = [None] * len(line)
    offset = len(line) - len(valid)
    for i, v in enumerate(sig_valid):
        sig[offset + i] = v
    hist: list[float | None] = [
        m - s if m is not None and s is not None else None for m, s in zip(line, sig, strict=True)
    ]
    return line, sig, hist


def bollinger(
    closes: list[float], period: int = 20, mult: float = 2.0
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """(upper, middle, lower)."""
    mid = sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        m = mid[i]
        if m is None:
            continue
        window = closes[i - period + 1 : i + 1]
        var = sum((x - m) ** 2 for x in window) / period
        sd = var**0.5
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return upper, mid, lower


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float | None]:
    """Wilder's Average True Range."""
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        )
    prev = sum(trs[1 : period + 1]) / period
    out[period] = prev
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out
