"""Money-weighted return (XIRR) via bisection. Floats, not Decimal — return
metrics are signals, not ledger entries (same policy as indicators/math.py)."""

from datetime import date

MIN_SPAN_DAYS = 30  # annualizing anything younger is noise
MAX_ITERATIONS = 200
TOLERANCE = 1e-9


def _npv(rate: float, flows: list[tuple[float, float]]) -> float:
    """flows = [(years_since_first, amount)]; rate > -1."""
    return sum(amount / (1.0 + rate) ** years for years, amount in flows)


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualized money-weighted return for dated cashflows.

    Convention: investments are negative, proceeds/terminal value positive.
    Returns None when the rate is undefined: no sign change, all flows one
    direction, or the span is under ~30 days.
    """
    if len(cashflows) < 2:
        return None
    ordered = sorted(cashflows, key=lambda cf: cf[0])
    first = ordered[0][0]
    span_days = (ordered[-1][0] - first).days
    if span_days < MIN_SPAN_DAYS:
        return None
    if not any(a < 0 for _, a in ordered) or not any(a > 0 for _, a in ordered):
        return None

    flows = [((d - first).days / 365.25, amount) for d, amount in ordered]

    lo, hi = -0.9999, 10.0
    npv_lo, npv_hi = _npv(lo, flows), _npv(hi, flows)
    if npv_lo * npv_hi > 0:
        hi = 100.0
        npv_hi = _npv(hi, flows)
        if npv_lo * npv_hi > 0:
            return None

    for _ in range(MAX_ITERATIONS):
        mid = (lo + hi) / 2
        npv_mid = _npv(mid, flows)
        if abs(hi - lo) < TOLERANCE:
            break
        if npv_lo * npv_mid <= 0:
            hi = mid
        else:
            lo, npv_lo = mid, npv_mid
    return (lo + hi) / 2
