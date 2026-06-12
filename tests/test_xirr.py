from datetime import date

from trd.services.xirr import xirr


def test_two_flow_exact_rate() -> None:
    # -1000 grows to 1210 in exactly 2 years → 10%/yr
    result = xirr([(date(2024, 1, 1), -1000.0), (date(2026, 1, 1), 1210.0)])
    assert result is not None
    assert abs(result - 0.10) < 1e-3


def test_flat_dca_near_zero() -> None:
    flows = [(date(2025, m, 15), -100.0) for m in range(1, 13)]
    flows.append((date(2025, 12, 31), 1200.0))
    result = xirr(flows)
    assert result is not None
    assert abs(result) < 1e-3


def test_losing_plan_negative() -> None:
    result = xirr([(date(2024, 1, 1), -1000.0), (date(2026, 1, 1), 810.0)])
    assert result is not None
    assert result < -0.05


def test_no_sign_change_returns_none() -> None:
    assert xirr([(date(2024, 1, 1), -100.0), (date(2025, 1, 1), -100.0)]) is None
    assert xirr([(date(2024, 1, 1), 100.0), (date(2025, 1, 1), 100.0)]) is None


def test_short_span_returns_none() -> None:
    assert xirr([(date(2026, 6, 1), -100.0), (date(2026, 6, 12), 105.0)]) is None


def test_single_flow_returns_none() -> None:
    assert xirr([(date(2026, 1, 1), -100.0)]) is None
