from datetime import datetime
from decimal import Decimal

from trd.models import Side, Transaction
from trd.services.fifo import fifo_position


def txn(side: Side, qty: str, price: str, fees: str = "0", when: int = 0) -> Transaction:
    return Transaction(
        id=when,
        account_id=1,
        instrument_id=1,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fees=Decimal(fees),
        executed_at=datetime(2026, 1, 1 + when),
    )


def test_single_buy_includes_fees() -> None:
    qty, cost = fifo_position([txn(Side.BUY, "10", "100", fees="5")])
    assert qty == Decimal(10)
    assert cost == Decimal(1005)


def test_full_sell_zeroes_position() -> None:
    qty, cost = fifo_position(
        [txn(Side.BUY, "10", "100", when=0), txn(Side.SELL, "10", "150", when=1)]
    )
    assert qty == 0
    assert cost == 0


def test_partial_sell_consumes_oldest_lot_first() -> None:
    # Lot 1: 10 @ 100. Lot 2: 10 @ 200. Sell 15 → 5 left from lot 2 at proportional cost.
    qty, cost = fifo_position(
        [
            txn(Side.BUY, "10", "100", when=0),
            txn(Side.BUY, "10", "200", when=1),
            txn(Side.SELL, "15", "300", when=2),
        ]
    )
    assert qty == Decimal(5)
    assert cost == Decimal(1000)  # 5 remaining of lot 2: 5/10 * 2000


def test_partial_lot_keeps_proportional_fees() -> None:
    # One lot: 10 @ 100 + 10 fees = 1010. Sell 5 → half the cost remains.
    qty, cost = fifo_position(
        [txn(Side.BUY, "10", "100", fees="10", when=0), txn(Side.SELL, "5", "120", when=1)]
    )
    assert qty == Decimal(5)
    assert cost == Decimal(505)


def test_fractional_quantities() -> None:
    qty, cost = fifo_position(
        [txn(Side.BUY, "0.5", "100000", when=0), txn(Side.SELL, "0.2", "110000", when=1)]
    )
    assert qty == Decimal("0.3")
    assert cost == Decimal("30000.0")
