"""Same-dates benchmark: what a set of contributions would be worth if every
dollar had instead bought the benchmark (SPY) on the same day. Because it uses
the exact dates of the real buys, date errors cancel in the relative comparison
— making "vs S&P 500" robust even when some lots carry estimated dates."""

from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal

from trd.models import Transaction
from trd.repos import PriceRepo

BENCHMARK = "SPY"


def same_dates_shares(
    flows: Iterable[tuple[date, Decimal]], close_on: Callable[[date], Decimal | None]
) -> Decimal | None:
    """Shares the flows would hold had each dollar bought the benchmark at its own
    day's close: a positive amount buys, a negative one sells. The one copy of
    this arithmetic — every same-days comparison in trd prices through it.
    None if any required close is missing: a partial count is not a smaller one."""
    shares = Decimal(0)
    for on, amount in flows:
        close = close_on(on)
        if close is None or close == 0:
            return None
        shares += amount / close
    return shares


def same_dates_value(
    prices: PriceRepo, benchmark_id: int, txns: list[Transaction]
) -> Decimal | None:
    """Current value of buying the benchmark with each contribution's amount on
    its own date. Returns None if any required close is missing."""
    if not txns:
        return None
    shares = same_dates_shares(
        [(t.executed_at.date(), t.quantity * t.price + t.fees) for t in txns],
        lambda on: prices.close_on_or_after(benchmark_id, on),
    )
    latest = prices.latest_close(benchmark_id)
    if shares is None or latest is None:
        return None
    return shares * latest
