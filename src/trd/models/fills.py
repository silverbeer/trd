"""Broker fills for a live engine: what the brokerage says became of its orders.

A live scan writes order intents and fills nothing (see OrderIntent). What the
broker did with them arrives as a snapshot file, shaped exactly like the
brokerage's own order and trade-approval reads so an agent can dump them with no
reshaping. Same split as reconciliation: the authenticated read happens outside
trd (docs/robinhood-mcp.md), and turning it into txns and positions is
deterministic arithmetic that needs no network and no credentials.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, computed_field, field_validator

from trd.models.core import Side
from trd.models.engine import IntentStatus

MARKET_TZ = ZoneInfo("America/New_York")


def exchange_local(value: datetime | None) -> datetime | None:
    """A broker timestamp as trd stores every other one: naive, exchange-local.

    The broker writes UTC ("...Z"); a txn's executed_at is a naive New York
    stamp, so a fill at 13:30Z must land as 09:30 or the ledger and the session
    arithmetic would put it on the wrong side of the open. A stamp that arrives
    naive is read as already exchange-local — the convention, not the host zone.
    """
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(MARKET_TZ).replace(tzinfo=None)


class BrokerOrderState(StrEnum):
    """Order states the brokerage reports. Anything else is read as still live."""

    QUEUED = "queued"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class _BrokerModel(BaseModel):
    # The brokerage returns far more than trd reads; unknown fields are ignored so
    # the snapshot can be written as-is.
    model_config = ConfigDict(extra="ignore")


class BrokerExecution(_BrokerModel):
    """One piece of a fill."""

    id: str | None = None
    price: Decimal
    quantity: Decimal
    timestamp: datetime
    fees: Decimal = Decimal(0)

    @field_validator("timestamp")
    @classmethod
    def _local(cls, value: datetime) -> datetime:
        local = exchange_local(value)
        assert local is not None
        return local


class BrokerOrder(_BrokerModel):
    """One equity order as the brokerage reports it."""

    id: str
    # Ours (the intent's ref_id) — or, with trade approvals on, the approval's id.
    ref_id: str | None = None
    symbol: str
    side: Side
    type: str | None = None
    state: str
    quantity: Decimal | None = None
    cumulative_quantity: Decimal = Decimal(0)
    average_price: Decimal | None = None
    fees: Decimal = Decimal(0)
    created_at: datetime | None = None
    last_transaction_at: datetime | None = None
    executions: list[BrokerExecution] = []
    reject_reason: str | None = Field(
        default=None, validation_alias=AliasChoices("reject_reason", "reason")
    )

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("state")
    @classmethod
    def _state(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("created_at", "last_transaction_at")
    @classmethod
    def _local(cls, value: datetime | None) -> datetime | None:
        return exchange_local(value)

    @field_validator("cumulative_quantity", "fees", mode="before")
    @classmethod
    def _zero_if_null(cls, value: object) -> object:
        return 0 if value is None else value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def executed_at(self) -> datetime | None:
        """When the fill completed: the last execution, else the order's last
        activity. None on an order nothing has executed against."""
        if self.executions:
            return max(e.timestamp for e in self.executions)
        return self.last_transaction_at


class ApprovalState(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    EXPIRED = "EXPIRED"


class ProposedOrder(_BrokerModel):
    symbol: str | None = None
    side: Side | None = None
    quantity: Decimal | None = None

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class BrokerApproval(_BrokerModel):
    """A trade approval: the order the broker is holding for a person to approve."""

    approval_id: str
    # "APPROVAL_STATE_PENDING" as the broker writes it, stored without the prefix.
    state: str
    expires_at: datetime | None = None
    proposed_order: ProposedOrder | None = None

    @field_validator("state")
    @classmethod
    def _state(cls, value: str) -> str:
        return value.strip().upper().removeprefix("APPROVAL_STATE_")

    @field_validator("expires_at")
    @classmethod
    def _local(cls, value: datetime | None) -> datetime | None:
        return exchange_local(value)


class FillSnapshot(_BrokerModel):
    """What the broker said about its orders and approvals, at `taken_at`."""

    taken_at: datetime | None = None
    orders: list[BrokerOrder] = []
    approvals: list[BrokerApproval] = []

    @field_validator("taken_at")
    @classmethod
    def _local(cls, value: datetime | None) -> datetime | None:
        return exchange_local(value)


class AppliedIntent(BaseModel):
    """One intent whose status this ingest changed."""

    intent_id: int
    symbol: str
    side: Side
    quantity: Decimal  # filled quantity on a fill, the intent's own otherwise
    price: Decimal | None = None  # the fill price; None when nothing filled
    from_status: IntentStatus
    to_status: IntentStatus
    broker_order_id: str | None = None
    note: str | None = None


class BookedPosition(BaseModel):
    """A position a fill opened, closed or trimmed."""

    position_id: int
    symbol: str
    strategy: str
    quantity: Decimal
    price: Decimal
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    rule: str | None = None
    pnl: Decimal | None = None
    r_multiple: Decimal | None = None


class FillConflict(BaseModel):
    """A broker answer trd will not apply, and why. Never written; always shown."""

    message: str
    symbol: str | None = None
    side: Side | None = None
    intent_id: int | None = None
    order_id: str | None = None
    approval_id: str | None = None


class UnmatchedBrokerItem(BaseModel):
    """An order or approval that belongs to no intent: a manual trade, another
    engine's, or history from before live mode. Reported, never adopted."""

    kind: str  # "order" | "approval"
    id: str
    symbol: str | None = None
    side: Side | None = None
    quantity: Decimal | None = None
    state: str


class FillIngestResult(BaseModel):
    account: str
    at: datetime
    taken_at: datetime | None = None
    orders_read: int = 0
    approvals_read: int = 0
    applied: list[AppliedIntent] = []
    opened: list[BookedPosition] = []
    closed: list[BookedPosition] = []
    trimmed: list[BookedPosition] = []
    expired: list[AppliedIntent] = []
    conflicts: list[FillConflict] = []
    unmatched: list[UnmatchedBrokerItem] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        """No conflicts. The one boolean a scheduled ingest exits on."""
        return not self.conflicts
