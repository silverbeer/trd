from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from trd.models import Side, Transaction


class OpenLot(BaseModel):
    """A surviving (possibly partially sold) buy lot after FIFO consumption."""

    bought_at: datetime
    price: Decimal  # original per-share price paid
    quantity: Decimal  # remaining shares from this lot
    cost: Decimal  # remaining cost basis, fees folded in


def open_lots(txns: list[Transaction]) -> list[OpenLot]:
    """Surviving lots for one instrument from chronological transactions.

    Buys append lots (fees fold into lot cost). Sells consume oldest lots first;
    a partially consumed lot keeps a proportional share of its cost.
    Oversells are clamped to zero — the service layer prevents them before insert,
    so a clamp here only matters for hand-edited data.
    """
    lots: list[OpenLot] = []
    for txn in txns:
        if txn.side == Side.BUY:
            lots.append(
                OpenLot(
                    bought_at=txn.executed_at,
                    price=txn.price,
                    quantity=txn.quantity,
                    cost=txn.quantity * txn.price + txn.fees,
                )
            )
        else:
            remaining = txn.quantity
            while remaining > 0 and lots:
                lot = lots[0]
                if lot.quantity <= remaining:
                    remaining -= lot.quantity
                    lots.pop(0)
                else:
                    fraction = (lot.quantity - remaining) / lot.quantity
                    lot.quantity -= remaining
                    lot.cost *= fraction
                    remaining = Decimal(0)
    return lots


def fifo_position(txns: list[Transaction]) -> tuple[Decimal, Decimal]:
    """Net (quantity, cost_basis) for one instrument — sum of surviving lots."""
    lots = open_lots(txns)
    quantity = sum((lot.quantity for lot in lots), Decimal(0))
    cost_basis = sum((lot.cost for lot in lots), Decimal(0))
    return quantity, cost_basis


class RealizedSale(BaseModel):
    """What a sell actually produced, once matched against the lots it consumed."""

    txn_id: int
    proceeds: Decimal  # what came in, net of the sell's own fees
    cost: Decimal  # FIFO-matched cost of exactly those shares

    @property
    def pnl(self) -> Decimal:
        return self.proceeds - self.cost

    @property
    def pnl_pct(self) -> Decimal | None:
        if self.cost == 0:
            return None
        return self.pnl / self.cost * 100


def realized_sales(txns: list[Transaction]) -> dict[int, RealizedSale]:
    """Realized result for every sell, keyed by transaction id.

    Same lot consumption as `open_lots` — this one keeps the cost it consumed
    instead of discarding it. Must be given the instrument's *whole* history for
    one account: FIFO cannot match a sale without the buys that preceded it, so
    filtering to a date window first would silently invent profits.
    """
    lots: list[OpenLot] = []
    out: dict[int, RealizedSale] = {}
    for txn in txns:
        if txn.side == Side.BUY:
            lots.append(
                OpenLot(
                    bought_at=txn.executed_at,
                    price=txn.price,
                    quantity=txn.quantity,
                    cost=txn.quantity * txn.price + txn.fees,
                )
            )
            continue
        remaining = txn.quantity
        matched = Decimal(0)
        while remaining > 0 and lots:
            lot = lots[0]
            if lot.quantity <= remaining:
                matched += lot.cost
                remaining -= lot.quantity
                lots.pop(0)
            else:
                consumed = remaining / lot.quantity
                matched += lot.cost * consumed
                lot.cost *= (lot.quantity - remaining) / lot.quantity
                lot.quantity -= remaining
                remaining = Decimal(0)
        out[txn.id] = RealizedSale(
            txn_id=txn.id, proceeds=txn.quantity * txn.price - txn.fees, cost=matched
        )
    return out
