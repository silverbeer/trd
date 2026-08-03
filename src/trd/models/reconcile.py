"""Broker reconciliation: what a brokerage says you hold vs what trd believes.

The gap is the number no backtest shows. A paper engine can be perfectly
consistent with itself and still describe a portfolio that does not exist — a
fill that never happened at the broker, a share count off by a rounding rule, a
mark taken from a stale bar.

The broker side arrives as a snapshot file rather than a live call on purpose.
Reading a brokerage is an authenticated, interactive, agent-side operation (see
docs/robinhood-mcp.md); the diff is deterministic arithmetic. Keeping them apart
means the comparison is reproducible, testable without a broker session, and
cannot smuggle a network call into a service that must never make one.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, computed_field, field_validator

# Fractional shares are stored to 6 decimals, so anything smaller than half a
# unit in the last place is a representation artifact, not a real disagreement.
QUANTITY_TOLERANCE = Decimal("0.0000005")


class ReconcileStatus(StrEnum):
    """What the two sides disagree about, if anything."""

    OK = "ok"
    QUANTITY = "quantity"  # both hold it, different sizes
    MISSING_AT_BROKER = "missing_at_broker"  # trd believes it holds; the broker does not
    UNTRACKED = "untracked"  # the broker holds it; trd has no idea


class BrokerPosition(BaseModel):
    """One holding as the broker reports it."""

    symbol: str
    quantity: Decimal
    price: Decimal | None = None  # the broker's own mark, if the snapshot carried one

    @field_validator("symbol")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper()


class BrokerSnapshot(BaseModel):
    """A point-in-time read of a brokerage account, written by whatever did the
    reading. `as_of` is load-bearing: a snapshot taken before the last engine
    fill will show a gap that is timing, not error."""

    as_of: datetime
    source: str = "robinhood"
    account: str | None = None  # the broker's own account label, for the header
    cash: Decimal | None = None
    positions: list[BrokerPosition] = []


class ReconcileRow(BaseModel):
    """One symbol, both sides, and what differs."""

    symbol: str
    broker_quantity: Decimal | None = None
    trd_quantity: Decimal | None = None
    broker_price: Decimal | None = None
    # trd's own mark and where it came from. A wide price gap with matching share
    # counts is a stale-data problem, not a bookkeeping one.
    trd_price: Decimal | None = None
    trd_price_date: date | None = None
    status: ReconcileStatus

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quantity_delta(self) -> Decimal | None:
        """Broker minus trd. Positive means the broker holds more."""
        if self.broker_quantity is None or self.trd_quantity is None:
            return None
        return self.broker_quantity - self.trd_quantity

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_delta_pct(self) -> Decimal | None:
        """How far trd's mark sits from the broker's, as a percentage of the
        broker's. This is the staleness reading, not a P&L number."""
        if self.broker_price is None or self.trd_price is None or self.broker_price == 0:
            return None
        return (self.trd_price - self.broker_price) / self.broker_price * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def matched(self) -> bool:
        return self.status == ReconcileStatus.OK


class Reconciliation(BaseModel):
    """The full diff, plus enough context to judge whether a gap is real."""

    account: str
    as_of: datetime  # when the broker was read
    source: str
    broker_cash: Decimal | None = None
    rows: list[ReconcileRow] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mismatches(self) -> int:
        return sum(1 for row in self.rows if not row.matched)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_sync(self) -> bool:
        """The whole answer in one boolean, so a scheduled check can exit on it."""
        return self.mismatches == 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def symbols_compared(self) -> int:
        return len(self.rows)
