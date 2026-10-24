"""An account's cash, and a staged allocation plan for it (SB-1383).

Built for the Fidelity Rollover IRA: a lump sum that lands as cash and is
invested in tranches over a year, by hand, at the broker. trd records what was
done and measures it. It never places anything, and never assumes a planned
tranche happened: a milestone is complete when the account's recorded buys say
so, and not before.

The arithmetic is in pure functions at the bottom of this module —
`milestone_dates`, `progress`, `drift_rows`, `propose` — so every number the
commands print is testable without a database or a provider.

Returns are deliberately modest about what they are. `simple_return_pct` is
gain over net deposits: not annualised, not time-weighted. XIRR (money-weighted,
annualised) appears only once the first deposit is 30 days old, the same floor
every other XIRR in trd uses. A time-weighted return needs a valuation history
this does not keep yet, so it is not offered.

A PACED target (SB-1388) — the ***REMOVED*** moonshot — sits outside the tranches: its
weight of the capital is earmarked and bought in monthly instalments on its own
clock. Until bought, that earmark is cash, reported as reserved and never added
to the cash it is part of. Its buys do not count toward any milestone, and it is
not in the drift sleeve; its own row says how far along the instalments it is.
"""

import calendar
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

import duckdb

from trd.errors import TrdError, UnknownAccountError
from trd.models import IncomeKind, Position, Side
from trd.models.alloc import (
    CASH,
    AllocEvent,
    AllocPlan,
    AllocTarget,
    AllocView,
    CashBalance,
    CashFlow,
    CashFlowKind,
    CashYield,
    Milestone,
    MilestoneProgress,
    MilestoneStatus,
    PacedRow,
    Proposal,
    ProposalLeg,
    TargetRow,
)
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, PriceRepo, TransactionRepo
from trd.repos.alloc import AllocRepo, CashFlowRepo
from trd.repos.income import IncomeRepo
from trd.services.benchmark import BENCHMARK, same_dates_shares
from trd.services.cashflow import external_flows
from trd.services.portfolio import PortfolioService
from trd.services.xirr import xirr

CENT = Decimal("0.01")
# A milestone counts as done when the buys cover it to within this share of its
# amount. Whole-share fills land a few dollars short of a round tranche; without
# the slack a $150,930 initial investment that bought $150,912 of funds would read
# "partial" for ever.
COMPLETE_TOLERANCE = Decimal("0.005")
DEFAULT_DRIFT_THRESHOLD = Decimal(5)
# A stated cash yield above this is a typo (35 for 3.5), not a money-market rate.
MAX_CASH_YIELD_PCT = Decimal(20)
# Income of these kinds is interest paid on the cash, not on a holding.
CASH_INTEREST_KINDS = (IncomeKind.INTEREST, IncomeKind.CASH_SWEEP)


class CashService:
    """Deposits, withdrawals and fees, and the cash balance they imply."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.accounts = AccountRepo(conn)
        self.flows = CashFlowRepo(conn)
        self.txns = TransactionRepo(conn)
        self.income = IncomeRepo(conn)

    def _account_id(self, account_name: str) -> int:
        account = self.accounts.get_by_name(account_name)
        if account is None:
            raise UnknownAccountError(account_name)
        return account.id

    def record(
        self,
        account_name: str,
        kind: CashFlowKind,
        amount: Decimal,
        occurred_at: datetime,
        note: str | None = None,
    ) -> CashFlow:
        """One deposit, withdrawal or fee. The amount is positive; the kind says which
        way it moved — a negative deposit would read as a withdrawal and quietly
        flip every return computed from it."""
        if amount <= 0:
            raise TrdError(f"Amount must be positive, got {amount}. Use the kind for direction.")
        return self.flows.add(
            self._account_id(account_name), kind, amount.quantize(CENT), occurred_at, note
        )

    def flows_for(self, account_name: str) -> list[CashFlow]:
        return self.flows.list_all(self._account_id(account_name))

    def balance(self, account_name: str) -> CashBalance:
        account_id = self._account_id(account_name)
        flows = self.flows.list_all(account_id)
        bought = sold = Decimal(0)
        for t in self.txns.list_chronological(account_id):
            if t.side == Side.BUY:
                bought += t.quantity * t.price + t.fees
            else:
                sold += t.quantity * t.price - t.fees
        return CashBalance(
            account=account_name,
            deposits=sum((f.amount for f in flows if f.kind == CashFlowKind.DEPOSIT), Decimal(0)),
            withdrawals=sum(
                (f.amount for f in flows if f.kind == CashFlowKind.WITHDRAWAL), Decimal(0)
            ),
            bought=bought,
            sold=sold,
            income=self.income.total(account_id),
            fees=sum((f.amount for f in flows if f.kind == CashFlowKind.FEE), Decimal(0)),
        )


class AllocService:
    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        provider: MarketDataProvider,
        snapshot: bool = True,
    ) -> None:
        """`snapshot=False` for a read-only connection: quotes are fetched but not
        stored (the unattended report)."""
        self.conn = conn
        self.provider = provider
        self.snapshot = snapshot
        self.accounts = AccountRepo(conn)
        self.repo = AllocRepo(conn)
        self.cash = CashService(conn)
        self.prices = PriceRepo(conn)
        self.portfolio = PortfolioService(conn, provider)

    # ------------------------------------------------------------------ plan

    def _account_id(self, account_name: str) -> int:
        account = self.accounts.get_by_name(account_name)
        if account is None:
            raise UnknownAccountError(account_name)
        return account.id

    def get(self, account_name: str) -> AllocPlan:
        account_id = self._account_id(account_name)
        row = self.repo.plan_row(account_id)
        if row is None:
            raise TrdError(f"No allocation plan on '{account_name}'. Run 'trd alloc set'.")
        return AllocPlan(
            id=row[0],
            account=account_name,
            account_id=account_id,
            name=row[1],
            account_kind=row[2],
            brokerage=row[3],
            capital=row[4],
            drift_threshold=row[5],
            created_at=row[6],
            note=row[7],
            targets=self.repo.targets(row[0]),
            milestones=self.repo.milestones(row[0]),
            cash_yield=self.repo.cash_yield(row[0]),
        )

    def default_account(self) -> str:
        ids = self.repo.plan_accounts()
        if len(ids) == 1:
            account = next(a for a in self.accounts.list_all() if a.id == ids[0])
            return account.name
        if not ids:
            raise TrdError("No allocation plans yet. Run 'trd alloc set'.")
        raise TrdError("More than one allocation plan — pass --account.")

    def create(
        self,
        account_name: str,
        name: str,
        capital: Decimal,
        targets: list[AllocTarget],
        account_kind: str | None = None,
        brokerage: str | None = None,
        drift_threshold: Decimal = DEFAULT_DRIFT_THRESHOLD,
        note: str | None = None,
        now: datetime | None = None,
        cash_yield: CashYield | None = None,
    ) -> AllocPlan:
        account_id = self._account_id(account_name)
        if self.repo.plan_row(account_id) is not None:
            raise TrdError(f"'{account_name}' already has a plan. Change it with 'trd alloc edit'.")
        validate_plan(capital, targets, drift_threshold)
        if cash_yield is not None:
            validate_cash_yield(cash_yield)
        at = now or datetime.now()
        plan_id = self.repo.create_plan(
            account_id, name, capital, account_kind, brokerage, drift_threshold, at, note
        )
        self.repo.replace_targets(plan_id, targets)
        if cash_yield is not None:
            self.repo.set_cash_yield(plan_id, cash_yield)
        self.repo.log(
            plan_id,
            at,
            f"created: capital {capital}, targets {_targets_text(targets)}"
            + (f", cash {_cash_yield_text(cash_yield)}" if cash_yield else ""),
        )
        return self.get(account_name)

    def edit(
        self,
        account_name: str,
        targets: list[AllocTarget] | None = None,
        capital: Decimal | None = None,
        drift_threshold: Decimal | None = None,
        name: str | None = None,
        note: str | None = None,
        now: datetime | None = None,
        cash_yield: CashYield | None = None,
        clear_cash_yield: bool = False,
    ) -> AllocPlan:
        plan = self.get(account_name)
        if cash_yield is not None and clear_cash_yield:
            raise TrdError("Pass a cash yield or --no-cash-yield, not both.")
        if cash_yield is not None:
            validate_cash_yield(cash_yield)
        new_targets = targets if targets is not None else plan.targets
        validate_plan(
            capital if capital is not None else plan.capital,
            new_targets,
            drift_threshold if drift_threshold is not None else plan.drift_threshold,
        )
        at = now or datetime.now()
        changes = []
        if targets is not None:
            self.repo.replace_targets(plan.id, targets)
            changes.append(f"targets {_targets_text(plan.targets)} → {_targets_text(targets)}")
        if capital is not None and capital != plan.capital:
            changes.append(f"capital {plan.capital} → {capital}")
        if drift_threshold is not None and drift_threshold != plan.drift_threshold:
            changes.append(f"drift threshold {plan.drift_threshold} → {drift_threshold}")
        if name is not None and name != plan.name:
            changes.append(f"name → {name}")
        old_yield = _cash_yield_text(plan.cash_yield) if plan.cash_yield else "none"
        if cash_yield is not None and cash_yield != plan.cash_yield:
            self.repo.set_cash_yield(plan.id, cash_yield)
            changes.append(f"cash yield {old_yield} → {_cash_yield_text(cash_yield)}")
        if clear_cash_yield and plan.cash_yield is not None:
            self.repo.clear_cash_yield(plan.id)
            changes.append(f"cash yield {old_yield} → none")
        if not changes and note is None:
            raise TrdError(
                "Nothing to change — pass --target, --capital, --drift, --name, --note "
                "or a cash yield."
            )
        self.repo.update_plan(
            plan.id, capital=capital, drift_threshold=drift_threshold, name=name, note=note
        )
        if changes:
            self.repo.log(plan.id, at, "; ".join(changes))
        return self.get(account_name)

    def schedule(
        self,
        account_name: str,
        start: date,
        initial: Decimal,
        amount: Decimal,
        every_months: int,
        count: int,
        replace: bool = False,
        now: datetime | None = None,
    ) -> AllocPlan:
        plan = self.get(account_name)
        if plan.milestones and not replace:
            raise TrdError("The plan already has a schedule. Pass --replace to rebuild it.")
        rows = milestone_dates(start, initial, amount, every_months, count)
        total = sum((r[2] for r in rows), Decimal(0))
        if total != plan.capital:
            gap = plan.capital - total
            raise TrdError(
                f"The schedule invests {total:,.2f} but the plan's capital is "
                f"{plan.capital:,.2f} — {gap:+,.2f} unaccounted for. Adjust --initial or "
                "--amount so they reconcile, or change the capital with 'trd alloc edit'."
            )
        self.repo.replace_milestones(plan.id, rows)
        self.repo.log(
            plan.id,
            now or datetime.now(),
            f"schedule: {initial} on {start}, then {count} x {amount} every {every_months} months",
        )
        return self.get(account_name)

    def _milestone(self, plan: AllocPlan, seq: int) -> Milestone:
        found = next((m for m in plan.milestones if m.seq == seq), None)
        if found is None:
            raise TrdError(f"No milestone {seq}. 'trd alloc show' lists them (0 = initial).")
        return found

    def postpone(
        self, account_name: str, seq: int, to: date, note: str | None = None, now=None
    ) -> AllocPlan:
        plan = self.get(account_name)
        m = self._milestone(plan, seq)
        if m.status == MilestoneStatus.CANCELLED:
            raise TrdError(f"Milestone {seq} is cancelled. Resume it first.")
        self.repo.update_milestone(m.id, due_date=to, status=MilestoneStatus.POSTPONED, note=note)
        self.repo.log(
            plan.id,
            now or datetime.now(),
            f"milestone {seq}: {m.due_date} → {to}" + (f" ({note})" if note else ""),
        )
        return self.get(account_name)

    def cancel(self, account_name: str, seq: int, note: str | None = None, now=None) -> AllocPlan:
        plan = self.get(account_name)
        m = self._milestone(plan, seq)
        self.repo.update_milestone(m.id, status=MilestoneStatus.CANCELLED, note=note)
        self.repo.log(
            plan.id,
            now or datetime.now(),
            f"milestone {seq} cancelled" + (f" ({note})" if note else ""),
        )
        return self.get(account_name)

    def resume(self, account_name: str, seq: int, now=None) -> AllocPlan:
        plan = self.get(account_name)
        m = self._milestone(plan, seq)
        self.repo.update_milestone(m.id, status=MilestoneStatus.PLANNED)
        self.repo.log(plan.id, now or datetime.now(), f"milestone {seq} back to planned")
        return self.get(account_name)

    def annotate(self, account_name: str, seq: int, note: str, now=None) -> AllocPlan:
        plan = self.get(account_name)
        m = self._milestone(plan, seq)
        self.repo.update_milestone(m.id, note=note)
        self.repo.log(plan.id, now or datetime.now(), f"milestone {seq} note: {note}")
        return self.get(account_name)

    def history(self, account_name: str) -> list[AllocEvent]:
        return self.repo.events(self.get(account_name).id)

    # ------------------------------------------------------------------ views

    def view(self, account_name: str, today: date | None = None) -> AllocView:
        plan = self.get(account_name)
        today = today or date.today()
        cash = self.cash.balance(account_name)
        positions = self.portfolio.positions(account_name, store_quotes=self.snapshot)
        rows, others = drift_rows(plan, positions)
        priced = [r for r in rows + others if r.quantity != 0]
        unpriced = sorted(r.symbol for r in priced if r.value is None)
        stale = sorted(r.symbol for r in priced if r.price_stale)
        holdings_value = (
            None if unpriced else sum((r.value or Decimal(0) for r in priced), Decimal(0))
        )
        paced_net = self._net_invested(plan, [t.symbol for t in plan.targets if t.paced])
        paced = paced_rows(plan, positions, paced_net, today)
        deployed = cash.bought - cash.sold - sum(paced_net.values(), Decimal(0))
        flows = self.cash.flows_for(account_name)
        view = AllocView(
            plan=plan,
            as_of=datetime.now(),
            cash=cash,
            rows=rows,
            other_holdings=others,
            unpriced=unpriced,
            stale=stale,
            progress=progress(plan.milestones, deployed, today, plan.fund_fraction),
            deployed=deployed,
            holdings_value=holdings_value,
            benchmark_value=self._benchmark(flows),
            day_change=_day_change(positions),
            paced=paced,
            cash_interest_earned=sum(
                (
                    i.amount
                    for i in self.cash.income.list_all(plan.account_id)
                    if i.kind in CASH_INTEREST_KINDS
                ),
                Decimal(0),
            ),
        )
        external = external_flows(flows)
        if view.total_value is not None and external:
            # Owner's flows only: a fee has already lowered total_value, and
            # counting it again as money handed back would excuse it.
            view.xirr = xirr(
                [(on, -float(amount)) for on, amount in external]
                + [(today, float(view.total_value))]
            )
        return view

    def _net_invested(self, plan: AllocPlan, symbols: list[str]) -> dict[str, Decimal]:
        """Net dollars put into each symbol: buys + fees - sale proceeds net of fees."""
        ids = {}
        for symbol in symbols:
            instrument = self.portfolio.instruments.get_by_symbol(symbol)
            if instrument is not None:
                ids[instrument.id] = symbol
        out = dict.fromkeys(symbols, Decimal(0))
        for t in self.cash.txns.list_chronological(plan.account_id):
            symbol = ids.get(t.instrument_id)
            if symbol is None:
                continue
            if t.side == Side.BUY:
                out[symbol] += t.quantity * t.price + t.fees
            else:
                out[symbol] -= t.quantity * t.price - t.fees
        return out

    def _benchmark(self, flows: list[CashFlow]) -> Decimal | None:
        """What the same deposits would be worth had each bought SPY on its day.
        Withdrawals sell SPY at that day's close; a fee does not (SPY charges
        none). None when a close is missing."""
        instrument = self.portfolio.instruments.get_by_symbol(BENCHMARK)
        external = external_flows(flows)
        if instrument is None or not external:
            return None
        shares = same_dates_shares(
            external,
            lambda on: self.prices.close_on_or_after(instrument.id, on),
        )
        latest = self.prices.latest_close(instrument.id)
        return shares * latest if shares is not None and latest is not None else None

    def proposal(
        self,
        account_name: str,
        amount: Decimal | None = None,
        seq: int | None = None,
        mode: str = "underweight",
        overrides: dict[str, Decimal] | None = None,
        today: date | None = None,
    ) -> Proposal:
        view = self.view(account_name, today)
        if amount is None:
            nxt = next_milestone(view.progress, seq)
            if nxt is None:
                raise TrdError("No open milestone to propose for. Pass --amount.")
            # Back to a whole-tranche amount: propose() holds the CASH and paced
            # shares back again, so what reaches the funds is what is still
            # missing. Scaled from the milestone's own amount, so an untouched
            # tranche proposes exactly its amount, not a cent off.
            share = (nxt.fund_target - nxt.invested_toward) / nxt.fund_target
            amount = (nxt.milestone.amount * share).quantize(CENT)
        quotes = self.provider.get_quotes([t.symbol for t in view.plan.targets if t.symbol != CASH])
        prices = {s: q.price for s, q in quotes.items()}
        return propose(view, amount, mode, prices, overrides or {})


# ------------------------------------------------------------- the arithmetic


def _day_change(positions: list[Position]) -> Decimal | None:
    """Today's move on what is held. None when any held quote lacks a previous
    close: a partial sum would read as the whole day."""
    changes = [p.day_change for p in positions if p.quantity != 0]
    if any(c is None for c in changes):
        return None
    return sum((c for c in changes if c is not None), Decimal(0))


def validate_plan(capital: Decimal, targets: list[AllocTarget], drift: Decimal) -> None:
    if capital <= 0:
        raise TrdError("Capital must be positive.")
    if not targets:
        raise TrdError("A plan needs at least one --target SYMBOL=WEIGHT.")
    seen = set()
    for t in targets:
        if t.symbol in seen:
            raise TrdError(f"{t.symbol} appears twice in the targets.")
        seen.add(t.symbol)
        if t.weight <= 0:
            raise TrdError(f"{t.symbol}: a target weight must be positive, got {t.weight}.")
        if t.paced:
            if t.symbol == CASH:
                raise TrdError("CASH cannot be paced — it is never bought.")
            if t.pace_months is None or t.pace_months < 1:
                raise TrdError(f"{t.symbol}: pace months must be at least 1.")
            if t.pace_start is None:
                raise TrdError(f"{t.symbol}: a paced target needs a start date.")
    total = sum((t.weight for t in targets), Decimal(0))
    if total != 100:
        raise TrdError(
            f"Target weights must sum to 100, got {total.normalize():f} ({_targets_text(targets)})."
        )
    if not 0 < drift < 100:
        raise TrdError(f"--drift is in percentage points and must be 0-100, got {drift}.")


def validate_cash_yield(cash_yield: CashYield) -> None:
    if not cash_yield.fund.strip():
        raise TrdError("--cash-fund needs a ticker, e.g. SPAXX.")
    if not 0 <= cash_yield.yield_pct <= MAX_CASH_YIELD_PCT:
        raise TrdError(
            f"--cash-yield is an annual percent and must be 0-{MAX_CASH_YIELD_PCT}, "
            f"got {cash_yield.yield_pct}."
        )


def resolve_cash_yield(
    current: CashYield | None,
    fund: str | None,
    yield_pct: Decimal | None,
    as_of: date | None,
    today: date,
) -> CashYield | None:
    """The cash yield a set/edit asks for, filling what was not passed from the
    one already stated. A new rate is dated today unless told otherwise — the
    date is what the report reads to call a rate stale. None = nothing asked."""
    if fund is None and yield_pct is None and as_of is None:
        return None
    fund = fund if fund is not None else current.fund if current else None
    pct = yield_pct if yield_pct is not None else current.yield_pct if current else None
    if fund is None or pct is None:
        raise TrdError("A cash yield needs both --cash-fund TICKER and --cash-yield PCT.")
    if as_of is None:
        as_of = current.as_of if current is not None and yield_pct is None else today
    return CashYield(fund=fund.strip().upper(), yield_pct=pct, as_of=as_of)


def _cash_yield_text(cash_yield: CashYield) -> str:
    return f"{cash_yield.fund} at {cash_yield.yield_pct.normalize():f}% (as of {cash_yield.as_of})"


def _targets_text(targets: list[AllocTarget]) -> str:
    return " ".join(
        f"{t.symbol}={t.weight.normalize():f}"
        + (f" (over {t.pace_months} months from {t.pace_start})" if t.paced else "")
        for t in targets
    )


def add_months(start: date, months: int) -> date:
    """Same day-of-month `months` later, clamped to the month's end (Jan 31 + 1
    month = Feb 28/29), never rolling into the month after."""
    month_index = start.month - 1 + months
    year, month = start.year + month_index // 12, month_index % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def milestone_dates(
    start: date, initial: Decimal, amount: Decimal, every_months: int, count: int
) -> list[tuple[int, date, Decimal]]:
    """seq 0 = the initial tranche on `start`; then `count` tranches of `amount`
    every `every_months` months. Dates come from the plan's start, never today."""
    if initial < 0 or amount < 0:
        raise TrdError("Tranche amounts cannot be negative.")
    if count < 0 or (count and every_months <= 0):
        raise TrdError("--count must be >= 0 and --every >= 1.")
    rows = [(0, start, initial.quantize(CENT))]
    rows += [
        (i, add_months(start, every_months * i), amount.quantize(CENT)) for i in range(1, count + 1)
    ]
    return rows


def progress(
    milestones: list[Milestone],
    deployed: Decimal,
    today: date,
    fund_fraction: Decimal = Decimal(1),
) -> list[MilestoneProgress]:
    """Each milestone against what has actually been invested.

    Buys are credited to milestones in order: the first dollars invested fill the
    initial tranche, the next fill milestone 1, and so on. A cancelled milestone
    takes none. Done / partial / overdue come from that, never from a flag.

    Each milestone is measured against its `fund_fraction` share: with a 5% CASH
    target, 95% of a tranche goes into funds and the rest stays cash on purpose.
    """
    out: list[MilestoneProgress] = []
    remaining = max(Decimal(0), deployed)
    cumulative = Decimal(0)
    for m in sorted(milestones, key=lambda m: m.seq):
        if m.status == MilestoneStatus.CANCELLED:
            out.append(
                MilestoneProgress(
                    milestone=m,
                    cumulative_planned=cumulative,
                    fund_target=Decimal(0),
                    invested_toward=Decimal(0),
                    state="cancelled",
                )
            )
            continue
        cumulative += m.amount
        fund_target = (m.amount * fund_fraction).quantize(CENT)
        toward = min(remaining, fund_target)
        remaining -= toward
        if fund_target == 0 or toward >= fund_target * (1 - COMPLETE_TOLERANCE):
            state = "completed"
        elif toward > 0:
            state = "partial"
        elif m.due_date < today:
            state = "overdue"
        elif m.status == MilestoneStatus.POSTPONED:
            state = "postponed"
        else:
            state = "planned"
        out.append(
            MilestoneProgress(
                milestone=m,
                cumulative_planned=cumulative,
                fund_target=fund_target,
                invested_toward=toward,
                state=state,
            )
        )
    return out


def next_milestone(
    rows: list[MilestoneProgress], seq: int | None = None
) -> MilestoneProgress | None:
    if seq is not None:
        return next((r for r in rows if r.milestone.seq == seq), None)
    return next(
        (r for r in rows if r.state in ("partial", "overdue", "planned", "postponed")), None
    )


def _invested_targets(plan: AllocPlan) -> dict[str, Decimal]:
    """Target weights over the tranche funds only, renormalised to 100 — what the
    money already in funds is measured against. CASH and paced targets are out:
    neither is filled by a tranche."""
    invested = [t for t in plan.targets if t.symbol != CASH and not t.paced]
    total = sum((t.weight for t in invested), Decimal(0))
    if total == 0:
        return {}
    return {t.symbol: t.weight / total * 100 for t in invested}


def drift_rows(
    plan: AllocPlan, positions: list[Position]
) -> tuple[list[TargetRow], list[TargetRow]]:
    """Every target (and every holding outside the plan) against the account.

    Drift is measured on the invested sleeve: a fund's share of the money in
    funds against its target renormalised over the funds. A held symbol with no
    price makes the sleeve unknowable, and drift is then left out rather than
    computed from a zero.
    """
    by_symbol = {p.instrument.symbol: p for p in positions if p.quantity != 0}
    weights = _invested_targets(plan)
    rows: list[TargetRow] = []
    for t in plan.targets:
        p = by_symbol.pop(t.symbol, None)
        rows.append(
            TargetRow(
                symbol=t.symbol,
                label=t.label,
                target_pct=t.weight,
                target_value=(plan.capital * t.weight / 100).quantize(CENT),
                quantity=p.quantity if p else Decimal(0),
                price=p.price if p else None,
                price_stale=p.price_stale if p else False,
                value=(p.market_value if p else Decimal(0)) if t.symbol != CASH else None,
                cost_basis=p.cost_basis if p else Decimal(0),
                day_change_pct=p.day_change_pct if p else None,
                invested_target_pct=weights.get(t.symbol),
            )
        )
    others = [
        TargetRow(
            symbol=s,
            target_pct=Decimal(0),
            target_value=Decimal(0),
            quantity=p.quantity,
            price=p.price,
            price_stale=p.price_stale,
            value=p.market_value,
            cost_basis=p.cost_basis,
            day_change_pct=p.day_change_pct,
        )
        for s, p in sorted(by_symbol.items())
    ]
    funds = [r for r in rows if r.invested_target_pct is not None]
    if any(r.quantity != 0 and r.value is None for r in funds):
        return rows, others
    sleeve = sum((r.value or Decimal(0) for r in funds), Decimal(0))
    if sleeve == 0:
        return rows, others
    for r in funds:
        r.invested_pct = (r.value or Decimal(0)) / sleeve * 100
        assert r.invested_target_pct is not None
        r.drift_pp = r.invested_pct - r.invested_target_pct
        if r.drift_pp <= -plan.drift_threshold:
            r.action = "underweight"
        elif r.drift_pp >= plan.drift_threshold:
            r.action = "overweight"
        else:
            r.action = "on target"
    return rows, others


def instalments(earmark: Decimal, months: int, start: date) -> list[tuple[date, Decimal]]:
    """`months` equal monthly instalments from `start`; the last takes the rounding
    so they sum to the earmark exactly."""
    each = (earmark / months).quantize(CENT, ROUND_DOWN)
    rows = [(add_months(start, i), each) for i in range(months)]
    last_date, _ = rows[-1]
    rows[-1] = (last_date, earmark - each * (months - 1))
    return rows


def paced_rows(
    plan: AllocPlan,
    positions: list[Position],
    net_invested: dict[str, Decimal],
    today: date,
) -> list[PacedRow]:
    by_symbol = {p.instrument.symbol: p for p in positions if p.quantity != 0}
    out = []
    for t in plan.targets:
        if not t.paced:
            continue
        assert t.pace_months is not None and t.pace_start is not None
        earmark = (plan.capital * t.weight / 100).quantize(CENT)
        steps = instalments(earmark, t.pace_months, t.pace_start)
        due = [amt for d, amt in steps if d <= today]
        p = by_symbol.get(t.symbol)
        out.append(
            PacedRow(
                symbol=t.symbol,
                label=t.label,
                weight=t.weight,
                earmark=earmark,
                months=t.pace_months,
                start=t.pace_start,
                instalment=steps[0][1],
                instalments_due=len(due),
                due_to_date=sum(due, Decimal(0)),
                invested=net_invested.get(t.symbol, Decimal(0)),
                next_date=next((d for d, _ in steps if d > today), None),
                quantity=p.quantity if p else Decimal(0),
                value=p.market_value if p else None,
                cost_basis=p.cost_basis if p else Decimal(0),
                day_change_pct=p.day_change_pct if p else None,
            )
        )
    return out


def propose(
    view: AllocView,
    amount: Decimal,
    mode: str,
    prices: dict[str, Decimal],
    overrides: dict[str, Decimal],
) -> Proposal:
    """Split `amount` across the plan. A suggestion to edit, never an order.

    The CASH target's share is held back as cash, and so is every paced
    target's: those are bought in their own instalments, listed in `paced_due`
    when one is due and unbought. The rest goes to the funds:

    - target: by target weight, whatever the account already holds.
    - underweight: toward the funds furthest below target after the deployment.
      With S = what the funds are worth now and D = what is being added, each
      fund's ideal is (S + D) x its renormalised weight; its gap is how far its
      value is below that (never negative — an overweight fund gets nothing).
      If the gaps add up to more than D, D is shared in proportion to the gaps;
      if less, every gap is filled and the remainder is split by weight.

    `overrides` replace a fund's amount outright (the person's edit); the rest is
    re-split over the other funds by the same rule.
    """
    if amount <= 0:
        raise TrdError("The deployment amount must be positive.")
    if mode not in ("target", "underweight"):
        raise TrdError("--mode is 'target' or 'underweight'.")
    plan = view.plan
    weights = _invested_targets(plan)
    unknown = set(overrides) - set(weights)
    if unknown:
        raise TrdError(f"Not a fund in this plan: {', '.join(sorted(unknown))}.")
    cash_weight = next((t.weight for t in plan.targets if t.symbol == CASH), Decimal(0))
    paced_weight = sum((t.weight for t in plan.targets if t.paced), Decimal(0))
    held_as_cash = (amount * cash_weight / 100).quantize(CENT)
    held_for_paced = (amount * paced_weight / 100).quantize(CENT)
    to_funds = amount - held_as_cash - held_for_paced
    notes: list[str] = []

    fixed = {s: v.quantize(CENT) for s, v in overrides.items()}
    free = to_funds - sum(fixed.values(), Decimal(0))
    if free < 0:
        raise TrdError(
            f"The edited amounts ({sum(fixed.values(), Decimal(0)):,.2f}) exceed what goes to "
            f"funds ({to_funds:,.2f})."
        )
    rest = {s: w for s, w in weights.items() if s not in fixed}
    rest_total = sum(rest.values(), Decimal(0))
    values = {r.symbol: r.value for r in view.rows if r.symbol in weights}

    split: dict[str, Decimal] = {}
    if rest and free > 0:
        if mode == "target":
            split = {s: free * w / rest_total for s, w in rest.items()}
        else:
            missing = [s for s in rest if values.get(s) is None]
            if missing:
                raise TrdError(
                    f"Underweight-first needs a price for every fund held; missing: "
                    f"{', '.join(missing)}. Use --mode target, or retry when quotes return."
                )
            sleeve = sum((values[s] or Decimal(0) for s in weights), Decimal(0))
            post = sleeve + to_funds
            gaps = {
                s: max(Decimal(0), post * weights[s] / 100 - (values[s] or Decimal(0)))
                for s in rest
            }
            gap_total = sum(gaps.values(), Decimal(0))
            if gap_total >= free and gap_total > 0:
                split = {s: free * g / gap_total for s, g in gaps.items()}
            else:
                spare = free - gap_total
                split = {s: gaps[s] + spare * rest[s] / rest_total for s in rest}
            over = [s for s, g in gaps.items() if g == 0]
            if over:
                notes.append(f"At or over target, so nothing new: {', '.join(over)}.")
    elif free > 0:
        notes.append(f"{free:,.2f} has no fund to go to: every fund's amount was set by hand.")

    amounts = {**fixed, **{s: v.quantize(CENT, ROUND_HALF_UP) for s, v in split.items()}}
    # Rounding to cents can leave a cent or two over or under; it goes to the
    # largest leg so the legs always sum to what was asked.
    drift_cents = to_funds - sum(amounts.values(), Decimal(0))
    if amounts and drift_cents and abs(drift_cents) <= Decimal("0.05") and split:
        largest = max(split, key=lambda s: amounts[s])
        amounts[largest] += drift_cents

    sleeve_now = sum((values.get(s) or Decimal(0) for s in weights), Decimal(0))
    sleeve_after = sleeve_now + sum(amounts.values(), Decimal(0))
    legs = []
    for s in weights:
        add = amounts.get(s, Decimal(0))
        before = values.get(s) or Decimal(0)
        price = prices.get(s)
        legs.append(
            ProposalLeg(
                symbol=s,
                amount=add,
                price=price,
                shares=(add / price).quantize(Decimal("0.001"), ROUND_DOWN) if price else None,
                weight_before=before / sleeve_now * 100 if sleeve_now else None,
                weight_after=(before + add) / sleeve_after * 100 if sleeve_after else None,
                target_pct=weights[s],
            )
        )
    available = view.cash.cash
    if amount > available:
        notes.append(
            f"This is {amount - available:,.2f} more than the account's recorded cash "
            f"({available:,.2f}). Record the deposit first ('trd cash deposit') — or the "
            "cash is not there yet."
        )
    if held_as_cash:
        notes.append(
            f"{held_as_cash:,.2f} ({cash_weight.normalize():f}% CASH target) stays uninvested."
        )
    if held_for_paced:
        names = ", ".join(t.symbol for t in plan.targets if t.paced)
        notes.append(
            f"{held_for_paced:,.2f} ({paced_weight.normalize():f}% for {names}) is not in this "
            "split — paced targets are bought in their own monthly instalments."
        )
    paced_due = []
    for row in view.paced:
        if row.behind <= 0:
            continue
        price = prices.get(row.symbol)
        paced_due.append(
            ProposalLeg(
                symbol=row.symbol,
                amount=row.behind,
                price=price,
                shares=(row.behind / price).quantize(Decimal("0.001"), ROUND_DOWN)
                if price
                else None,
            )
        )
    missing_prices = [s for s in weights if s not in prices and amounts.get(s)]
    if missing_prices:
        notes.append(f"No quote for {', '.join(missing_prices)}: no share estimate.")
    return Proposal(
        plan=plan.name,
        mode=mode,
        amount=amount,
        cash_available=available,
        legs=legs,
        held_as_cash=held_as_cash,
        held_for_paced=held_for_paced,
        paced_due=paced_due,
        notes=notes,
    )
