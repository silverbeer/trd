"""The 'tons of details' view for DCA plans: contribution log, per-symbol
lifetime stats with weight drift, cadence health, and money-weighted return."""

import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.models import Transaction
from trd.providers.base import MarketDataProvider
from trd.services.fifo import fifo_position
from trd.services.plan import Plan, PlanService, PlanStatus
from trd.services.xirr import xirr


class ContributionLeg(BaseModel):
    symbol: str
    quantity: Decimal
    price: Decimal
    amount: Decimal  # quantity * price + fees


class ContributionEvent(BaseModel):
    date: date
    legs: list[ContributionLeg]

    @property
    def total(self) -> Decimal:
        return sum((leg.amount for leg in self.legs), Decimal(0))


class SymbolStat(BaseModel):
    symbol: str
    invested: Decimal
    quantity: Decimal
    avg_cost: Decimal | None
    price: Decimal | None
    value: Decimal | None
    target_weight: Decimal | None  # percent, from plan_allocation
    actual_weight: Decimal | None  # percent of current plan value

    @property
    def pl(self) -> Decimal | None:
        if self.value is None:
            return None
        return self.value - self.invested

    @property
    def pl_pct(self) -> Decimal | None:
        pl = self.pl
        if pl is None or self.invested == 0:
            return None
        return pl / self.invested * 100

    @property
    def drift(self) -> Decimal | None:
        """actual minus target weight, in percentage points. Positive = overweight."""
        if self.target_weight is None or self.actual_weight is None:
            return None
        return self.actual_weight - self.target_weight


class CadenceHealth(BaseModel):
    day_of_month: int | None
    first_month: date | None
    last_invested: date | None
    months_invested: int
    expected_months: int
    streak: int

    @property
    def missed(self) -> int:
        return max(0, self.expected_months - self.months_invested)

    @property
    def next_due(self) -> date | None:
        """Next scheduled contribution date (day_of_month clamped to month end)."""
        if self.first_month is None:
            return None
        today = date.today()
        day = self.day_of_month or calendar.monthrange(today.year, today.month)[1]
        this_month_due = date(
            today.year, today.month, min(day, calendar.monthrange(today.year, today.month)[1])
        )
        if self.last_invested is not None and (
            self.last_invested.year,
            self.last_invested.month,
        ) == (today.year, today.month):
            year, month = (
                (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            )
            return date(year, month, min(day, calendar.monthrange(year, month)[1]))
        return this_month_due


class PlanDetail(BaseModel):
    plan: Plan
    status: PlanStatus
    events: list[ContributionEvent]
    symbol_stats: list[SymbolStat]
    cadence: CadenceHealth
    xirr: float | None


def _due_months(first: date, today: date, day_of_month: int | None) -> list[tuple[int, int]]:
    """Calendar months from first txn month through today where a contribution
    was due. Current month counts only once its scheduled day has passed."""
    months: list[tuple[int, int]] = []
    year, month = first.year, first.month
    while (year, month) < (today.year, today.month):
        months.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    last_dom = calendar.monthrange(today.year, today.month)[1]
    due_day = min(day_of_month or last_dom, last_dom)
    if today.day >= due_day:
        months.append((today.year, today.month))
    return months


class DcaDetailService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.plans = PlanService(conn, provider)

    def detail(self, account_name: str) -> PlanDetail:
        plan = self.plans.get_plan(account_name)
        status = self.plans.status(account_name)  # single quote fetch, reused below
        txns = self.plans.txns.list_for_plan(plan.id)

        events = self._events(txns)
        symbol_stats = self._symbol_stats(plan, status, txns)
        cadence = self._cadence(plan, txns)
        plan_xirr = self._xirr(txns, status)
        return PlanDetail(
            plan=plan,
            status=status,
            events=events,
            symbol_stats=symbol_stats,
            cadence=cadence,
            xirr=plan_xirr,
        )

    def _events(self, txns: list[Transaction]) -> list[ContributionEvent]:
        by_date: dict[date, list[Transaction]] = defaultdict(list)
        for txn in txns:
            by_date[txn.executed_at.date()].append(txn)
        events = []
        for when in sorted(by_date):
            legs = [
                ContributionLeg(
                    symbol=self._symbol(t.instrument_id),
                    quantity=t.quantity,
                    price=t.price,
                    amount=t.quantity * t.price + t.fees,
                )
                for t in by_date[when]
            ]
            legs.sort(key=lambda leg: leg.amount, reverse=True)
            events.append(ContributionEvent(date=when, legs=legs))
        return events

    def _symbol(self, instrument_id: int) -> str:
        instrument = self.plans.instruments.get(instrument_id)
        return instrument.symbol if instrument else "?"

    def _symbol_stats(
        self, plan: Plan, status: PlanStatus, txns: list[Transaction]
    ) -> list[SymbolStat]:
        by_instrument: dict[int, list[Transaction]] = defaultdict(list)
        for txn in txns:
            by_instrument[txn.instrument_id].append(txn)

        symbols = {i: self._symbol(i) for i in by_instrument}
        quotes = self.provider.get_quotes(list(symbols.values()))

        stats: list[SymbolStat] = []
        for instrument_id, instrument_txns in by_instrument.items():
            symbol = symbols[instrument_id]
            quantity, cost_basis = fifo_position(instrument_txns)
            invested = sum((t.quantity * t.price + t.fees for t in instrument_txns), Decimal(0))
            quote = quotes.get(symbol)
            value = quote.price * quantity if quote else None
            stats.append(
                SymbolStat(
                    symbol=symbol,
                    invested=invested,
                    quantity=quantity,
                    avg_cost=cost_basis / quantity if quantity else None,
                    price=quote.price if quote else None,
                    value=value,
                    target_weight=plan.allocations.get(symbol),
                    actual_weight=None,  # filled below once total is known
                )
            )
        total_value = (
            sum((s.value for s in stats), Decimal(0))
            if all(s.value is not None for s in stats)
            else None
        )
        if total_value:
            for stat in stats:
                assert stat.value is not None
                stat.actual_weight = stat.value / total_value * 100
        stats.sort(key=lambda s: s.invested, reverse=True)
        return stats

    def _cadence(self, plan: Plan, txns: list[Transaction]) -> CadenceHealth:
        invested_months = {(t.executed_at.year, t.executed_at.month) for t in txns}
        if not txns:
            return CadenceHealth(
                day_of_month=plan.day_of_month,
                first_month=None,
                last_invested=None,
                months_invested=0,
                expected_months=0,
                streak=0,
            )
        first = min(t.executed_at.date() for t in txns)
        last = max(t.executed_at.date() for t in txns)
        due = _due_months(first, date.today(), plan.day_of_month)
        streak = 0
        for month in reversed(due):
            if month in invested_months:
                streak += 1
            else:
                break
        return CadenceHealth(
            day_of_month=plan.day_of_month,
            first_month=first.replace(day=1),
            last_invested=last,
            months_invested=len(invested_months),
            expected_months=len(due),
            streak=streak,
        )

    def _xirr(self, txns: list[Transaction], status: PlanStatus) -> float | None:
        if status.value is None or not txns:
            return None
        flows: list[tuple[date, float]] = []
        for txn in txns:
            amount = float(txn.quantity * txn.price + txn.fees)
            flows.append((txn.executed_at.date(), -amount if txn.side == "buy" else amount))
        flows.append((date.today(), float(status.value)))
        return xirr(flows)
