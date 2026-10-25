"""Broker reconciliation: what a brokerage says you hold vs what trd believes.

The gap is the number no backtest shows. A paper engine can be perfectly
consistent with itself and still describe a portfolio that does not exist — a
fill that never happened at the broker, a share count off by a rounding rule, a
mark taken from a stale bar.

The broker side arrives as a snapshot file rather than a live call on purpose.
Reading a brokerage is an authenticated, interactive, agent-side operation (see
docs/broker-mcp.md); the diff is deterministic arithmetic. Keeping them apart
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

# How far the broker's cash may sit from the engine's ledger before it is a
# mismatch. Five cents absorbs a fractional fill's sub-cent rounding repeated
# over a few trades; a real gap (a fee trd never saw, a fill it missed) is
# dollars, not cents.
CASH_TOLERANCE = Decimal("0.05")


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
    # Settled buying power, and sale proceeds still waiting to settle. Both
    # optional: a snapshot without them reconciles positions only. Compared with
    # the engine's ledger only under `--compare-cash` — see CashCheck.
    cash: Decimal | None = None
    unsettled: Decimal | None = None
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


class CashCheck(BaseModel):
    """The broker's cash against the engine's settled-cash ledger.

    Opt-in (`trd engine reconcile --compare-cash`) because it is only meaningful
    when the brokerage account's cash is the engine's and nothing else's. The
    account that funds a live engine routinely holds more than the engine's
    capital, and comparing that balance with a ledger that starts from the
    capital would report the surplus as a "mismatch" every day. Turn
    it on for an account dedicated to the engine; leave it off otherwise.
    """

    trd_settled: Decimal
    trd_unsettled: Decimal
    broker_settled: Decimal
    broker_unsettled: Decimal | None = None
    tolerance: Decimal = CASH_TOLERANCE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def settled_delta(self) -> Decimal:
        """Broker minus trd. Positive means the broker has more to spend."""
        return self.broker_settled - self.trd_settled

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unsettled_delta(self) -> Decimal | None:
        if self.broker_unsettled is None:
            return None
        return self.broker_unsettled - self.trd_unsettled

    @computed_field  # type: ignore[prop-decorator]
    @property
    def matched(self) -> bool:
        if abs(self.settled_delta) > self.tolerance:
            return False
        delta = self.unsettled_delta
        return delta is None or abs(delta) <= self.tolerance


class Reconciliation(BaseModel):
    """The full diff, plus enough context to judge whether a gap is real."""

    account: str
    as_of: datetime  # when the broker was read
    source: str
    broker_cash: Decimal | None = None
    broker_unsettled: Decimal | None = None
    rows: list[ReconcileRow] = []
    # Present only when asked for with --compare-cash.
    cash: CashCheck | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mismatches(self) -> int:
        return sum(1 for row in self.rows if not row.matched)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cash_mismatch(self) -> bool:
        return self.cash is not None and not self.cash.matched

    @computed_field  # type: ignore[prop-decorator]
    @property
    def in_sync(self) -> bool:
        """The whole answer in one boolean, so a scheduled check can exit on it."""
        return self.mismatches == 0 and not self.cash_mismatch

    @computed_field  # type: ignore[prop-decorator]
    @property
    def symbols_compared(self) -> int:
        return len(self.rows)
