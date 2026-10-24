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
# A paced target this close to its schedule is on it (SB-1390).
PACED_TOLERANCE = Decimal(1)


class CashFlowKind(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    # An advisory/management fee the account paid (SB-1418). Money out, like a
    # withdrawal, but NOT the owner's money moving: it is a cost of the account,
    # so it lowers the return. A withdrawal is excused from performance (the
    # owner took it); a fee must never be.
    FEE = "fee"


# What `trd cash ls` calls each kind. A fee is spelled out: it is the line the
# owner most needs to see and least expects.
CASH_FLOW_LABELS = {
    CashFlowKind.DEPOSIT: "deposit",
    CashFlowKind.WITHDRAWAL: "withdrawal",
    CashFlowKind.FEE: "advisory fee",
}


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
        """The change in the account's CASH: a deposit adds, a withdrawal and a
        fee take away. Not a performance flow — see `cashflow.external_flows`."""
        return self.amount if self.kind == CashFlowKind.DEPOSIT else -self.amount

    @property
    def external(self) -> bool:
        """Whether the owner moved this money. Deposits and withdrawals are; a fee
        is not, which is what keeps it inside the return."""
        return self.kind in (CashFlowKind.DEPOSIT, CashFlowKind.WITHDRAWAL)


class CashBalance(BaseModel):
    """An account's cash, derived — never stored."""

    account: str
    deposits: Decimal
    withdrawals: Decimal
    bought: Decimal  # cost + fees of every buy
    sold: Decimal  # proceeds - fees of every sell
    income: Decimal
    fees: Decimal = Decimal(0)  # advisory fees charged to the account (money out)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_deposits(self) -> Decimal:
        return self.deposits - self.withdrawals

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cash(self) -> Decimal:
        return self.deposits - self.withdrawals - self.fees - self.bought + self.sold + self.income


class MilestoneStatus(StrEnum):
    """What the person decided. Done / partial / overdue is derived, not stored."""

    PLANNED = "planned"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class AllocTarget(BaseModel):
    symbol: str
    weight: Decimal  # percent
    label: str | None = None
    # PACED (SB-1388): the weight is earmarked from the plan's capital and bought
    # in equal monthly instalments over pace_months from pace_start, outside the
    # tranches. None = an ordinary target the tranches fill.
    pace_months: int | None = None
    pace_start: date | None = None

    @property
    def paced(self) -> bool:
        return self.pace_months is not None


class Milestone(BaseModel):
    id: int
    seq: int  # 0 = the initial tranche
    due_date: date
    amount: Decimal
    status: MilestoneStatus = MilestoneStatus.PLANNED
    note: str | None = None


class CashYield(BaseModel):
    """What the plan's uninvested cash earns while it waits (SB-1417): the core
    money-market fund it sits in and that fund's yield, as read on `as_of`. A
    stated rate, never a measurement — interest actually paid is income."""

    fund: str
    yield_pct: Decimal  # annual, percent
    as_of: date


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
    cash_yield: CashYield | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fund_fraction(self) -> Decimal:
        """The share of every tranche meant for funds: 1 less the CASH target and
        every paced target, which are funded on their own clock."""
        held = sum((t.weight for t in self.targets if t.symbol == CASH or t.paced), Decimal(0))
        return 1 - held / 100

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
    day_change_pct: Decimal | None = None  # vs previous close; None without one
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


class PacedRow(BaseModel):
    """A paced target against what has been bought of it.

    The earmark is weight x capital. Instalments are earmark / months on the
    same day of each month from the start, the last absorbing the rounding.
    `invested` is net dollars put in (buys + fees - sells), so a sale releases
    earmark back rather than reading as progress.
    """

    symbol: str
    label: str | None = None
    weight: Decimal
    earmark: Decimal
    months: int
    start: date
    instalment: Decimal
    instalments_due: int  # instalment dates on or before today
    due_to_date: Decimal
    invested: Decimal
    next_date: date | None = None  # next instalment after today; None once all are past
    quantity: Decimal = Decimal(0)
    value: Decimal | None = None
    cost_basis: Decimal = Decimal(0)
    day_change_pct: Decimal | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reserved(self) -> Decimal:
        """Earmarked and not yet bought: cash held for this, not free cash."""
        return max(Decimal(0), self.earmark - self.invested)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def behind(self) -> Decimal:
        """Due by today and not yet bought — what to buy now to be on schedule.
        Under a dollar is nothing: a dollar order that fills a few cents short
        of its instalment is on schedule, not behind."""
        short = self.due_to_date - self.invested
        return short if short >= PACED_TOLERANCE else Decimal(0)

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
    deployed: Decimal  # net cost of tranche funds bought (buys - sells), paced targets excluded
    holdings_value: Decimal | None  # None when anything held is unpriced
    benchmark_value: Decimal | None = None  # the same deposits in SPY, same days
    xirr: float | None = None
    day_change: Decimal | None = None  # today's move on what is held; None if unknown
    paced: list[PacedRow] = []
    # Interest the account has actually been paid on its cash: recorded income of
    # kind interest or cash_sweep. Not derived from the stated yield.
    cash_interest_earned: Decimal = Decimal(0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cash_monthly_interest(self) -> Decimal | None:
        """What the cash earns a month at the stated yield: cash x yield / 12.
        All of it, reserved included — it all sits in the core position. None
        without a stated yield; an estimate, since a money-market rate moves."""
        cy = self.plan.cash_yield
        if cy is None:
            return None
        return max(Decimal(0), self.cash.cash) * cy.yield_pct / 100 / 12

    @computed_field  # type: ignore[prop-decorator]
    @property
    def reserved_cash(self) -> Decimal:
        """Cash earmarked for paced targets — part of `cash`, never added to it."""
        return sum((p.reserved for p in self.paced), Decimal(0))

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


# ------------------------------------------------------------- the report (SB-1416)


class Score(BaseModel):
    """Dollars put in on one base, and what they are worth now.

    The base is either the account's net DEPOSITS (the whole account, cash and
    all) or the net dollars INVESTED (buys less sales). A benchmark scored on a
    base is the same dollars bought into it on the same days, so the two rows
    differ only in what the money was put into. Returns are simple — gain over
    the base, not annualised and not time-weighted. `periods` maps "1W"/"1M" to
    a return over that window, present only once the account is that old.
    """

    base: Decimal
    value: Decimal | None  # None = a price is missing, never zero
    today_pct: Decimal | None = None
    periods: dict[str, Decimal | None] = {}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gain(self) -> Decimal | None:
        if self.value is None:
            return None
        return self.value - self.base

    @computed_field  # type: ignore[prop-decorator]
    @property
    def return_pct(self) -> Decimal | None:
        if self.gain is None or self.base == 0:
            return None
        return self.gain / self.base * 100


class BenchmarkScore(BaseModel):
    """A market index the account is measured against, on both bases.

    `deposits`: every deposit bought into it on its day — what the whole account
    is compared with. `invested`: every dollar actually invested, on the day it
    was invested — what the money in funds is compared with. The target mix has
    no deposits score: it is a mix of the funds, not a place to park cash.
    """

    key: str
    label: str
    short: str  # the message's name for it
    symbol: str | None = None  # None for the target mix
    deposits: Score | None = None
    invested: Score


class FundScore(BaseModel):
    """One holding on the leaderboard."""

    symbol: str
    label: str | None = None
    paced: bool = False
    quantity: Decimal
    value: Decimal | None
    cost_basis: Decimal
    today_pct: Decimal | None = None
    weight_pct: Decimal | None = None  # share of everything invested

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gain(self) -> Decimal | None:
        if self.value is None:
            return None
        return self.value - self.cost_basis

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gain_pct(self) -> Decimal | None:
        if self.gain is None or self.cost_basis == 0:
            return None
        return self.gain / self.cost_basis * 100


class AllocReport(BaseModel):
    """`trd alloc report`: the view, scored against the market (SB-1416).

    `account` is the whole account on net deposits; `invested` is the holdings
    on the net cost of what was bought (price return: income is in `account`,
    not here, as the benchmarks are priced on closes without dividends).
    """

    view: AllocView
    today: date
    funded_on: date | None = None  # first deposit
    periods: list[str] = []  # "1W" / "1M" the account is old enough for
    account: Score
    invested: Score
    benchmarks: list[BenchmarkScore] = []
    funds: list[FundScore] = []  # best since bought first

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cash_pct(self) -> Decimal | None:
        total = self.view.total_value
        if not total:
            return None
        return self.view.cash.cash / total * 100


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
    held_for_paced: Decimal = Decimal(0)  # paced targets' share: bought on their own clock
    paced_due: list[ProposalLeg] = []  # paced instalments due and not yet bought
    notes: list[str] = []
