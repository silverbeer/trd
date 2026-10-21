"""A live engine's outbox: the orders it wants placed, shaped for the broker.

A live scan writes order intents and places nothing. An agent session drains
them: `trd engine outbox` hands over each pending intent as the parameters the
broker's order tool takes, and `trd engine outbox ack` records what the broker
answered. trd never calls the broker itself (docs/robinhood-mcp.md) — it emits a
spec and reads back an id, which is the whole of its side of the conversation.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from trd.models.core import Side
from trd.models.engine import IntentStatus, OrderIntent


class OutboxOrder(BaseModel):
    """One pending intent as an order spec. The first block is what the broker's
    place call takes, unchanged; the rest is what a person approving it wants."""

    intent_id: int
    # The broker's idempotency key. Minted once with the intent; a retry of the
    # same order reuses it, so a re-send cannot make a second order.
    ref_id: str
    symbol: str
    side: Side
    quantity: str  # 6dp, the precision the broker takes fractional shares in
    type: str = "market"
    time_in_force: str = "gfd"
    market_hours: str = "regular_hours"

    ref_price: Decimal  # the price the scan saw, not a quote
    notional: Decimal
    strategy: str
    rule: str | None = None  # the exit rule, on a sell
    reason: str
    created_at: datetime
    # The 16:00 ET close of the intent's session: an approval lapses then, and an
    # intent nobody placed is swept as expired.
    expires_at: datetime
    # Written at or after 15:45 ET: placed now, an approval has minutes to land.
    late: bool = False
    note: str | None = None


class WithheldOrder(BaseModel):
    """A pending intent the outbox will not hand over, and why: a hard cap it
    breaks now (caps can be lowered after an intent was written), or a quote too
    old to size an order from. Listed so a person can see what was held back —
    never in `orders`, which is the list an agent places."""

    order: OutboxOrder
    reason: str
    stale: bool = False


class Outbox(BaseModel):
    account: str
    at: datetime
    orders: list[OutboxOrder] = []
    # The kill switch. While it is on, `orders` is empty whatever is pending —
    # an agent that reads only `orders` places nothing, which is the point.
    halted: bool = False
    halt_reason: str | None = None
    withheld: list[WithheldOrder] = []


class OutboxAck(BaseModel):
    """What one `outbox ack` did. `changed` is False when the same answer was
    already recorded — an ack is safe to repeat."""

    symbol: str
    from_status: IntentStatus
    to_status: IntentStatus
    changed: bool
    intent: OrderIntent
