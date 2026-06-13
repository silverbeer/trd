"""Same-dates benchmark: what a set of contributions would be worth if every
dollar had instead bought the benchmark (SPY) on the same day. Because it uses
the exact dates of the real buys, date errors cancel in the relative comparison
— making "vs S&P 500" robust even when some lots carry estimated dates."""

from decimal import Decimal

from trd.models import Transaction
from trd.repos import PriceRepo

BENCHMARK = "SPY"


def same_dates_value(
    prices: PriceRepo, benchmark_id: int, txns: list[Transaction]
) -> Decimal | None:
    """Current value of buying the benchmark with each contribution's amount on
    its own date. Returns None if any required close is missing."""
    if not txns:
        return None
    shares = Decimal(0)
    for txn in txns:
        close = prices.close_on_or_after(benchmark_id, txn.executed_at.date())
        if close is None or close == 0:
            return None
        shares += (txn.quantity * txn.price + txn.fees) / close
    latest = prices.latest_close(benchmark_id)
    if latest is None:
        return None
    return shares * latest
