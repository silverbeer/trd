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
"""

import calendar
from datetime import date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

import duckdb

from trd.errors import TrdError, UnknownAccountError
from trd.models import Position, Side
from trd.models.alloc import (
    CASH,
    AllocEvent,
    AllocPlan,
    AllocTarget,
    AllocView,
    CashBalance,
    CashFlow,
    CashFlowKind,
    Milestone,
    MilestoneProgress,
    MilestoneStatus,
    Proposal,
    ProposalLeg,
    TargetRow,
)
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, PriceRepo, TransactionRepo
from trd.repos.alloc import AllocRepo, CashFlowRepo
from trd.repos.income import IncomeRepo
from trd.services.benchmark import BENCHMARK
from trd.services.portfolio import PortfolioService
from trd.services.xirr import xirr

CENT = Decimal("0.01")
# A milestone counts as done when the buys cover it to within this share of its
# amount. Whole-share fills land a few dollars short of a round tranche; without
# the slack a $150,930 initial investment that bought $150,912 of funds would read
# "partial" for ever.
COMPLETE_TOLERANCE = Decimal("0.005")
DEFAULT_DRIFT_THRESHOLD = Decimal(5)


class CashService:
    """Deposits and withdrawals, and the cash balance they imply."""

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
        """One deposit or withdrawal. The amount is positive; the kind says which
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
        )


class AllocService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
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
    ) -> AllocPlan:
        account_id = self._account_id(account_name)
        if self.repo.plan_row(account_id) is not None:
            raise TrdError(f"'{account_name}' already has a plan. Change it with 'trd alloc edit'.")
        validate_plan(capital, targets, drift_threshold)
        at = now or datetime.now()
        plan_id = self.repo.create_plan(
            account_id, name, capital, account_kind, brokerage, drift_threshold, at, note
        )
        self.repo.replace_targets(plan_id, targets)
        self.repo.log(plan_id, at, f"created: capital {capital}, targets {_targets_text(targets)}")
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
    ) -> AllocPlan:
        plan = self.get(account_name)
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
        if not changes and note is None:
            raise TrdError(
                "Nothing to change — pass --target, --capital, --drift, --name or --note."
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
        positions = self.portfolio.positions(account_name)
        rows, others = drift_rows(plan, positions)
        priced = [r for r in rows + others if r.quantity != 0]
        unpriced = sorted(r.symbol for r in priced if r.value is None)
        stale = sorted(r.symbol for r in priced if r.price_stale)
        holdings_value = (
            None if unpriced else sum((r.value or Decimal(0) for r in priced), Decimal(0))
        )
        deployed = cash.bought - cash.sold
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
        )
        if view.total_value is not None and flows:
            view.xirr = xirr(
                [(f.occurred_at.date(), -float(f.signed)) for f in flows]
                + [(today, float(view.total_value))]
            )
        return view

    def _benchmark(self, flows: list[CashFlow]) -> Decimal | None:
        """What the same deposits would be worth had each bought SPY on its day.
        Withdrawals sell SPY at that day's close. None when a close is missing."""
        instrument = self.portfolio.instruments.get_by_symbol(BENCHMARK)
        if instrument is None or not flows:
            return None
        shares = Decimal(0)
        for flow in flows:
            close = self.prices.close_on_or_after(instrument.id, flow.occurred_at.date())
            if close is None or close == 0:
                return None
            shares += flow.signed / close
        latest = self.prices.latest_close(instrument.id)
        return shares * latest if latest is not None else None

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
            # Back to a whole-tranche amount: propose() holds the CASH share back
            # again, so what reaches the funds is exactly what is still missing.
            fraction = view.plan.fund_fraction
            amount = ((nxt.fund_target - nxt.invested_toward) / fraction).quantize(CENT)
        quotes = self.provider.get_quotes([t.symbol for t in view.plan.targets if t.symbol != CASH])
        prices = {s: q.price for s, q in quotes.items()}
        return propose(view, amount, mode, prices, overrides or {})


# ------------------------------------------------------------- the arithmetic


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
    total = sum((t.weight for t in targets), Decimal(0))
    if total != 100:
        raise TrdError(
            f"Target weights must sum to 100, got {total.normalize():f} ({_targets_text(targets)})."
        )
    if not 0 < drift < 100:
        raise TrdError(f"--drift is in percentage points and must be 0-100, got {drift}.")


def _targets_text(targets: list[AllocTarget]) -> str:
    return " ".join(f"{t.symbol}={t.weight.normalize():f}" for t in targets)


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
    """Target weights over the invested targets only, renormalised to 100 —
    what the money already in funds is measured against."""
    invested = [t for t in plan.targets if t.symbol != CASH]
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
        )
        for s, p in sorted(by_symbol.items())
    ]
    funds = [r for r in rows if r.symbol != CASH]
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


def propose(
    view: AllocView,
    amount: Decimal,
    mode: str,
    prices: dict[str, Decimal],
    overrides: dict[str, Decimal],
) -> Proposal:
    """Split `amount` across the plan. A suggestion to edit, never an order.

    The CASH target's share is held back as cash. The rest goes to the funds:

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
    held_as_cash = (amount * cash_weight / 100).quantize(CENT)
    to_funds = amount - held_as_cash
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
        notes=notes,
    )
