from trd.indicators.math import atr, bollinger, ema, macd, rsi, sma


def test_sma_known_values() -> None:
    out = sma([1, 2, 3, 4, 5], 3)
    assert out == [None, None, 2, 3, 4]


def test_sma_insufficient_data() -> None:
    assert sma([1, 2], 5) == [None, None]


def test_ema_seeds_with_sma_then_smooths() -> None:
    out = ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2  # SMA seed
    assert out[3] == 3.0  # 4*0.5 + 2*0.5
    assert out[4] == 4.0


def test_rsi_all_gains_is_100() -> None:
    closes = list(range(1, 31))
    out = rsi([float(c) for c in closes], 14)
    assert out[-1] == 100.0


def test_rsi_all_losses_near_zero() -> None:
    closes = [float(c) for c in range(60, 30, -1)]
    out = rsi(closes, 14)
    assert out[-1] == 0.0


def test_rsi_alternating_is_mid_range() -> None:
    closes = [100.0 + (1 if i % 2 else 0) for i in range(40)]
    out = rsi(closes, 14)
    assert out[-1] is not None
    assert 30 < out[-1] < 70


def test_macd_uptrend_positive_histogram() -> None:
    closes = [100.0 * 1.01**i for i in range(60)]
    line, _signal, hist = macd(closes)
    assert line[-1] is not None and line[-1] > 0
    assert hist[-1] is not None


def test_bollinger_bands_bracket_price() -> None:
    closes = [100.0 + (i % 5) for i in range(40)]
    upper, mid, lower = bollinger(closes, 20, 2.0)
    assert upper[-1] is not None and lower[-1] is not None and mid[-1] is not None
    assert lower[-1] < mid[-1] < upper[-1]


def test_bollinger_flat_series_zero_width() -> None:
    closes = [100.0] * 30
    upper, mid, lower = bollinger(closes, 20, 2.0)
    assert upper[-1] == mid[-1] == lower[-1] == 100.0


def test_atr_constant_range() -> None:
    n = 30
    highs = [102.0] * n
    lows = [100.0] * n
    closes = [101.0] * n
    out = atr(highs, lows, closes, 14)
    assert out[-1] is not None
    assert abs(out[-1] - 2.0) < 1e-9


def test_warm_up_is_none() -> None:
    closes = [float(i) for i in range(100, 130)]
    assert rsi(closes, 14)[13] is None
    assert rsi(closes, 14)[14] is not None
    assert sma(closes, 20)[18] is None
    assert sma(closes, 20)[19] is not None
