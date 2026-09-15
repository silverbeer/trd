"""The cash that went in and the cash that came back, in one definition.

Three separate places built an XIRR flow series out of transactions — the
dashboard, the equity curve and a DCA plan's detail — each with its own copy of
"a buy is negative, a sell is positive, fees count". Three copies is how income
gets added to two of them and quietly missed by the third, and a return that is
right on one screen and wrong on the next is worse than one that is wrong
everywhere.

So the rule here matches the one that keeps `exit_quantity` shared between the
live scanner and the backtest: the arithmetic lives once.

**What a dividend does to a return.** Money leaving your pocket is negative and
money arriving is positive, and a dividend is money arriving — the same sign as
a sale, without the share count changing. That is the whole of it, and it is why
a price-only return understates the truth rather than merely differing from it.

The terminal value is NOT added here. Each caller closes its own series: the
dashboard with the portfolio's market value today, the equity curve with the end
of its window, a plan with what the plan is worth. Folding that in would need
this module to know which of the three it was serving.
"""

from collections.abc import Iterable, Sequence
from datetime import date

from trd.models import Income, Transaction
from trd.services.fifo import fifo_position


def trade_flows(
    txns: Iterable[Transaction],
    after: date | None = None,
    until: date | None = None,
) -> list[tuple[date, float]]:
    """Buys out, sells in, fees always a cost to whoever is paying them.

    `after` is exclusive and `until` inclusive, matching the equity curve's
    window, where the opening holdings are already carried as a single outflow
    and a trade on the boundary date would otherwise be counted twice.
    """
    flows: list[tuple[date, float]] = []
    for txn in txns:
        on = txn.executed_at.date()
        if after is not None and on <= after:
            continue
        if until is not None and on > until:
            continue
        amount = float(txn.quantity * txn.price)
        fees = float(txn.fees)
        flows.append((on, -(amount + fees) if txn.side == "buy" else amount - fees))
    return flows


def income_flows(
    income: Iterable[Income],
    after: date | None = None,
    until: date | None = None,
) -> list[tuple[date, float]]:
    """Dividends, interest and sweep payments — always positive, never a lot.

    Income has no quantity and no price, so nothing here can reach the holdings.
    That separation is the point of storing it apart from `txn`.
    """
    flows: list[tuple[date, float]] = []
    for payment in income:
        on = payment.received_at.date()
        if after is not None and on <= after:
            continue
        if until is not None and on > until:
            continue
        flows.append((on, float(payment.amount)))
    return flows


def cash_flows(
    txns: Iterable[Transaction],
    income: Iterable[Income] = (),
    after: date | None = None,
    until: date | None = None,
) -> list[tuple[date, float]]:
    """Both sides of the ledger, oldest first.

    Sorted because XIRR is solved over a series and a caller appending its
    terminal value should not have to think about ordering.
    """
    flows = trade_flows(txns, after, until) + income_flows(income, after, until)
    flows.sort(key=lambda f: f[0])
    return flows


def plan_income_flows(
    income: Iterable[Income],
    plan_txns: Sequence[Transaction],
    account_txns: Sequence[Transaction],
) -> list[tuple[date, float]]:
    """A plan's share of the income its holdings paid.

    A dividend lands on a *holding*, and an account's holding of a symbol is
    rarely all the plan's. The sofi account holds SPY from two 2025 one-off buys
    and from one plan contribution; crediting the plan with the whole SPY
    dividend would flatter it, and crediting none of it would understate it.

    So each payment is split by the plan's share of that symbol on the day it
    was paid — plan shares over account shares, both by FIFO as of that date.
    Symbols the plan does not hold contribute nothing, which is what keeps a VOO
    dividend out of a plan that has never bought VOO.

    Account-level income (interest, a cash sweep) carries no instrument and is
    never attributed: it is paid on idle cash, not on anything the plan bought.
    """
    by_instrument: dict[int, list[Transaction]] = {}
    plan_by_instrument: dict[int, list[Transaction]] = {}
    for txn in account_txns:
        by_instrument.setdefault(txn.instrument_id, []).append(txn)
    for txn in plan_txns:
        plan_by_instrument.setdefault(txn.instrument_id, []).append(txn)

    flows: list[tuple[date, float]] = []
    for payment in income:
        iid = payment.instrument_id
        if iid is None or iid not in plan_by_instrument:
            continue
        on = payment.received_at.date()
        held, _ = fifo_position([t for t in by_instrument.get(iid, []) if _upto(t, on)])
        mine, _ = fifo_position([t for t in plan_by_instrument[iid] if _upto(t, on)])
        if held <= 0 or mine <= 0:
            continue
        flows.append((on, float(payment.amount) * float(mine / held)))
    flows.sort(key=lambda f: f[0])
    return flows


def _upto(txn: Transaction, on: date) -> bool:
    return txn.executed_at.date() <= on
