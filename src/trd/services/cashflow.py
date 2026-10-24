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

**What a fee does to a return.** An advisory fee is money the ACCOUNT paid, not
money the owner took out. A withdrawal is excused from performance — the owner
has the cash — so it is an inflow in an XIRR series and is added back before a
gain is measured. A fee booked the same way would be excused too, and the
managed account would look exactly as good as an unmanaged one charging
nothing. So a fee is never an external flow: in a cash-based return the
terminal value is simply lower by it (`external_flows` leaves it out), and in a
trade-based return, where account cash is not part of the value, it is money
the owner paid (`fee_flows`, negative, like a buy that bought nothing).

The terminal value is NOT added here. Each caller closes its own series: the
dashboard with the portfolio's market value today, the equity curve with the end
of its window, a plan with what the plan is worth. Folding that in would need
this module to know which of the three it was serving.
"""

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from trd.models import Income, Transaction
from trd.models.alloc import CashFlow, CashFlowKind
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
    return [(on, -float(amount)) for on, amount in trade_amounts(txns, after, until)]


def trade_amounts(
    txns: Iterable[Transaction],
    after: date | None = None,
    until: date | None = None,
) -> list[tuple[date, Decimal]]:
    """Dollars put to work, in Decimal: a buy's cost plus fees is positive, a
    sale's proceeds less fees negative. `trade_flows` is this from the pocket's
    side, as floats for XIRR; a same-days benchmark prices exactly these amounts
    (SB-1416), so the two can never disagree about what a trade cost."""
    out: list[tuple[date, Decimal]] = []
    for txn in txns:
        on = txn.executed_at.date()
        if after is not None and on <= after:
            continue
        if until is not None and on > until:
            continue
        amount = txn.quantity * txn.price
        out.append((on, amount + txn.fees if txn.side == "buy" else -(amount - txn.fees)))
    return out


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


def fee_flows(
    flows: Iterable[CashFlow],
    after: date | None = None,
    until: date | None = None,
) -> list[tuple[date, float]]:
    """Fees the account paid, as money out of the owner's pocket — negative.

    For the TRADE-based returns (dashboard, equity curve), whose value is the
    holdings alone: the fee left the account's cash, which those views never
    see, so without this line it would vanish from the return altogether.
    Deposits and withdrawals in the same iterable are ignored.
    """
    out: list[tuple[date, float]] = []
    for flow in flows:
        if flow.kind != CashFlowKind.FEE:
            continue
        on = flow.occurred_at.date()
        if after is not None and on <= after:
            continue
        if until is not None and on > until:
            continue
        out.append((on, -float(flow.amount)))
    return out


def external_flows(flows: Iterable[CashFlow]) -> list[tuple[date, Decimal]]:
    """Money the OWNER moved: a deposit positive, a withdrawal negative, oldest
    first as given. Fees are left out on purpose — see the module docstring.

    For the CASH-based returns (an allocation plan's XIRR, its same-deposits
    benchmark, the daily report), whose value is holdings plus cash: the fee has
    already lowered the cash, so leaving it out of the flows is exactly what
    makes it lower the return.
    """
    return [(f.occurred_at.date(), f.signed) for f in flows if f.external]


def cash_flows(
    txns: Iterable[Transaction],
    income: Iterable[Income] = (),
    after: date | None = None,
    until: date | None = None,
    fees: Iterable[CashFlow] = (),
) -> list[tuple[date, float]]:
    """Both sides of the ledger, oldest first.

    Sorted because XIRR is solved over a series and a caller appending its
    terminal value should not have to think about ordering.
    """
    flows = (
        trade_flows(txns, after, until)
        + income_flows(income, after, until)
        + fee_flows(fees, after, until)
    )
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
