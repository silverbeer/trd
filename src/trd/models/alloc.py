"""Cash moved into an account, and a staged allocation plan for it (SB-1383).

The plan is a plan: target weights and a schedule. What actually happened is
read from the account's recorded trades and deposits, so nothing here can say
money was invested that was not.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, computed_field

# A target that means "hold this share as uninvested cash". A Treasury fund held
# for the same reason is its own ticker: priced, and drifting like any other.
CASH = "CASH"


class CashFlowKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


class CashFlow(BaseModel):
    """Money moved into or out of an account. Not a trade: it creates no lot."""

    id: int
    account_id: int
    kind: CashFlowKind
    amount: Decimal  # always positive; kind carries the direction
    occurred_at: datetime
    note: str | None = None

    @property
    def signed(self) -> Decimal:
        return self.amount if self.kind == CashFlowKind.DEPOSIT else -self.amount


class CashBalance(BaseModel):
    """An account's cash, derived — never stored."""

    account: str
    deposits: Decimal
    withdrawals: Decimal
    bought: Decimal  # cost + fees of every buy
    sold: Decimal  # proceeds - fees of every sell
    income: Decimal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_deposits(self) -> Decimal:
        return self.deposits - self.withdrawals

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cash(self) -> Decimal:
        return self.deposits - self.withdrawals - self.bought + self.sold + self.income


class MilestoneStatus(StrEnum):
    """What the person decided. Done / partial / overdue is derived, not stored."""

    PLANNED = "planned"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class AllocTarget(BaseModel):
    symbol: str
    weight: Decimal  # percent
    label: str | None = None


class Milestone(BaseModel):
    id: int
    seq: int  # 0 = the initial tranche
    due_date: date
    amount: Decimal
    status: MilestoneStatus = MilestoneStatus.PLANNED
    note: str | None = None


class AllocPlan(BaseModel):
    id: int
    account: str
    account_id: int
    name: str
    account_kind: str | None = None
    brokerage: str | None = None
    capital: Decimal
    drift_threshold: Decimal = Decimal(5)
    created_at: datetime
    note: str | None = None
    targets: list[AllocTarget] = []
    milestones: list[Milestone] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fund_fraction(self) -> Decimal:
        """The share of every tranche meant for funds: 1 less the CASH target."""
        cash = sum((t.weight for t in self.targets if t.symbol == CASH), Decimal(0))
        return 1 - cash / 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def scheduled(self) -> Decimal:
        """Everything the schedule plans to invest, cancelled milestones excluded."""
        return sum(
            (m.amount for m in self.milestones if m.status != MilestoneStatus.CANCELLED),
            Decimal(0),
        )


class AllocEvent(BaseModel):
    at: datetime
    what: str


# ------------------------------------------------------------------- the views


class MilestoneProgress(BaseModel):
    """One milestone against what the account has actually invested."""

    milestone: Milestone
    cumulative_planned: Decimal  # this one and every earlier live one
    # The part of `amount` meant for funds: a CASH target's share of every tranche
    # stays cash by design, so investing the rest IS the milestone done.
    fund_target: Decimal
    invested_toward: Decimal  # fund dollars bought toward THIS milestone
    state: str  # completed | partial | overdue | planned | postponed | cancelled


class TargetRow(BaseModel):
    """One target against the account as it stands."""

    symbol: str
    label: str | None = None
    target_pct: Decimal
    target_value: Decimal  # target_pct of the plan's capital
    quantity: Decimal = Decimal(0)
    price: Decimal | None = None
    price_stale: bool = False
    value: Decimal | None = None  # None = held but unpriced, never zero
    cost_basis: Decimal = Decimal(0)
    # Weight among what has been INVESTED, against the target renormalised to the
    # invested targets. While a staged plan is still deploying, most of the
    # account is cash waiting by design; measuring against the whole account
    # would call every fund underweight for a year.
    invested_pct: Decimal | None = None
    invested_target_pct: Decimal | None = None
    drift_pp: Decimal | None = None
    action: str | None = None  # underweight | on target | overweight

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gain(self) -> Decimal | None:
        if self.value is None or self.quantity == 0:
            return None
        return self.value - self.cost_basis


class AllocView(BaseModel):
    """`trd alloc show`: the plan, the account, and how far apart they are."""

    plan: AllocPlan
    as_of: datetime
    cash: CashBalance
    rows: list[TargetRow]
    other_holdings: list[TargetRow] = []  # held, but not in the plan
    unpriced: list[str] = []
    stale: list[str] = []
    progress: list[MilestoneProgress] = []
    deployed: Decimal  # net cost of securities bought (buys - sells)
    holdings_value: Decimal | None  # None when anything held is unpriced
    benchmark_value: Decimal | None = None  # the same deposits in SPY, same days
    xirr: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_value(self) -> Decimal | None:
        if self.holdings_value is None:
            return None
        return self.holdings_value + self.cash.cash

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gain(self) -> Decimal | None:
        """Dollars made: what the account is worth minus what was put in."""
        if self.total_value is None or self.cash.net_deposits == 0:
            return None
        return self.total_value - self.cash.net_deposits

    @computed_field  # type: ignore[prop-decorator]
    @property
    def simple_return_pct(self) -> Decimal | None:
        """gain / net deposits. Simple, not annualised, and not time-weighted —
        stated as such wherever it is shown."""
        if self.gain is None:
            return None
        return self.gain / self.cash.net_deposits * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining_to_deploy(self) -> Decimal:
        """Still to go into funds: the schedule less the CASH target's share
        (which stays cash) less what has been bought."""
        return max(Decimal(0), self.plan.scheduled * self.plan.fund_fraction - self.deployed)


class ProposalLeg(BaseModel):
    symbol: str
    amount: Decimal
    price: Decimal | None = None
    shares: Decimal | None = None  # an estimate at `price`; the fill decides
    weight_before: Decimal | None = None  # % of invested sleeve
    weight_after: Decimal | None = None
    target_pct: Decimal | None = None  # renormalised over invested targets


class Proposal(BaseModel):
    """How a deployment could be split. A suggestion to review, never an order."""

    plan: str
    mode: str  # target | underweight
    amount: Decimal
    cash_available: Decimal
    legs: list[ProposalLeg]
    held_as_cash: Decimal = Decimal(0)  # the CASH target's share
    notes: list[str] = []
