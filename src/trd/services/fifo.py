from decimal import Decimal

from trd.models import Side, Transaction


def fifo_position(txns: list[Transaction]) -> tuple[Decimal, Decimal]:
    """Net (quantity, cost_basis) for one instrument from chronological transactions.

    Buys append lots (fees fold into lot cost). Sells consume oldest lots first;
    a partially consumed lot keeps a proportional share of its cost.
    Oversells are clamped to zero — the service layer prevents them before insert,
    so a clamp here only matters for hand-edited data.
    """
    lots: list[list[Decimal]] = []  # [remaining_qty, remaining_cost]
    for txn in txns:
        if txn.side == Side.BUY:
            lots.append([txn.quantity, txn.quantity * txn.price + txn.fees])
        else:
            remaining = txn.quantity
            while remaining > 0 and lots:
                lot_qty, lot_cost = lots[0]
                if lot_qty <= remaining:
                    remaining -= lot_qty
                    lots.pop(0)
                else:
                    fraction = (lot_qty - remaining) / lot_qty
                    lots[0] = [lot_qty - remaining, lot_cost * fraction]
                    remaining = Decimal(0)
    quantity = sum((lot[0] for lot in lots), Decimal(0))
    cost_basis = sum((lot[1] for lot in lots), Decimal(0))
    return quantity, cost_basis
