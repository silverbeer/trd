"""Portfolio equity curve — value over time.

Computed entirely from data trd already stores: FIFO holdings from `txn` as of each
date, valued at `price_daily` adjusted closes. Nothing is snapshotted; the curve is
derived on demand, so it's always consistent with the ledger.

FIFO runs per (account, instrument) — a sell only consumes lots in its own account —
then values aggregate per instrument across the scope.
"""

from bisect import bisect_right
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError, UnknownAccountError
from trd.models import AccountType, Transaction
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo, TransactionRepo
from trd.services.fifo import fifo_position
from trd.services.xirr import xirr


class EquityPoint(BaseModel):
    date: date
    value: Decimal  # market value of holdings
    cost_basis: Decimal  # FIFO cost of open lots held that day
    drawdown_pct: float  # vs the running peak, <= 0
    day_pnl: Decimal = Decimal(0)  # flow-adjusted market P&L vs the prior point
    day_pnl_pct: float | None = None  # day_pnl as % of the prior day's value

    @property
    def unrealized_pl(self) -> Decimal:
        return self.value - self.cost_basis


class EquityCurve(BaseModel):
    account_label: str
    points: list[EquityPoint]
    max_drawdown_pct: float
    period_return_pct: float | None  # end value vs start value
    pl_pct: float | None  # end value vs end cost basis (unrealized)
    xirr: float | None
    unpriced: list[str] = []  # symbols with no price history in range

    @property
    def start_date(self) -> date:
        return self.points[0].date

    @property
    def end_date(self) -> date:
        return self.points[-1].date

    @property
    def start_value(self) -> Decimal:
        return self.points[0].value

    @property
    def end_value(self) -> Decimal:
        return self.points[-1].value

    @property
    def peak_value(self) -> Decimal:
        return max(p.value for p in self.points)


class EquityCurveService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.txns = TransactionRepo(conn)
        self.prices = PriceRepo(conn)

    def _scope(
        self, account_name: str | None, include_simulation: bool
    ) -> tuple[int | None, set[int], str]:
        if account_name is not None:
            account = self.accounts.get_by_name(account_name)
            if account is None:
                raise UnknownAccountError(account_name)
            return account.id, set(), account.name
        skip: set[int] = set()
        if not include_simulation:
            skip = {a.id for a in self.accounts.list_all() if a.type == AccountType.SIMULATION}
        return None, skip, "all accounts"

    def curve(
        self,
        account_name: str | None = None,
        lookback_days: int | None = None,
        include_simulation: bool = False,
    ) -> EquityCurve:
        account_id, skip, label = self._scope(account_name, include_simulation)
        txns = [t for t in self.txns.list_chronological(account_id) if t.account_id not in skip]
        if not txns:
            raise TrdError("No transactions to chart. Record a buy first.")

        by_pair: dict[tuple[int, int], list[Transaction]] = defaultdict(list)
        for t in txns:
            by_pair[(t.account_id, t.instrument_id)].append(t)
        instrument_ids = {iid for _, iid in by_pair}

        today = date.today()
        first_txn = min(t.executed_at.date() for t in txns)
        window_start = first_txn
        if lookback_days is not None:
            window_start = max(first_txn, today - timedelta(days=lookback_days))

        # Per-instrument price series (adjusted), loaded once; carry forward between bars.
        price_dates: dict[int, list[date]] = {}
        price_vals: dict[int, list[Decimal]] = {}
        unpriced: list[str] = []
        trading_dates: set[date] = set()
        for iid in instrument_ids:
            series = self.prices.closes_in_range(iid, first_txn - timedelta(days=7), today)
            if not series:
                inst = self.instruments.get(iid)
                unpriced.append(inst.symbol if inst else str(iid))
                continue
            price_dates[iid] = [d for d, _ in series]
            price_vals[iid] = [v for _, v in series]
            trading_dates.update(d for d, _ in series if window_start <= d <= today)

        if not trading_dates:
            raise TrdError(
                "No price history in range. Run 'trd sync' (use --years N for a longer curve)."
            )
        dates = sorted(trading_dates)

        def close_asof(iid: int, d: date) -> Decimal | None:
            ds = price_dates.get(iid)
            if not ds:
                return None
            i = bisect_right(ds, d)
            return price_vals[iid][i - 1] if i > 0 else None

        # Net external cash invested per day (buys add, sell proceeds subtract) — used to
        # strip contributions out of the day-over-day P&L so it reflects only price moves.
        flow_by_date: dict[date, Decimal] = defaultdict(Decimal)
        for t in txns:
            amount = t.quantity * t.price
            if t.side.value == "buy":
                flow_by_date[t.executed_at.date()] += amount + t.fees
            else:
                flow_by_date[t.executed_at.date()] -= amount - t.fees

        points: list[EquityPoint] = []
        peak = Decimal(0)
        max_dd = 0.0
        prev_value: Decimal | None = None
        # Holdings change only on transaction dates — recompute FIFO lazily.
        pair_qty: dict[tuple[int, int], Decimal] = {}
        pair_cost: dict[tuple[int, int], Decimal] = {}
        ti = 0
        sorted_txns = sorted(txns, key=lambda t: t.executed_at)
        dirty = True
        for d in dates:
            while ti < len(sorted_txns) and sorted_txns[ti].executed_at.date() <= d:
                ti += 1
                dirty = True
            if dirty:
                for pair, ptxns in by_pair.items():
                    upto = [t for t in ptxns if t.executed_at.date() <= d]
                    qty, cost = fifo_position(upto)
                    pair_qty[pair] = qty
                    pair_cost[pair] = cost
                dirty = False

            value = Decimal(0)
            cost_basis = Decimal(0)
            for (_, iid), qty in pair_qty.items():
                if qty == 0:
                    continue
                price = close_asof(iid, d)
                if price is not None:
                    value += qty * price
            for cost in pair_cost.values():
                cost_basis += cost

            if value > peak:
                peak = value
            dd = float((value - peak) / peak * 100) if peak > 0 else 0.0
            max_dd = min(max_dd, dd)

            # Flow-adjusted day P&L: value move minus the day's net contributions.
            day_pnl = Decimal(0)
            day_pnl_pct: float | None = None
            if prev_value is not None:
                day_pnl = value - prev_value - flow_by_date.get(d, Decimal(0))
                if prev_value > 0:
                    day_pnl_pct = float(day_pnl / prev_value * 100)
            points.append(
                EquityPoint(
                    date=d,
                    value=value,
                    cost_basis=cost_basis,
                    drawdown_pct=dd,
                    day_pnl=day_pnl,
                    day_pnl_pct=day_pnl_pct,
                )
            )
            prev_value = value

        start_value = points[0].value
        end_value = points[-1].value
        end_cost = points[-1].cost_basis
        period_return = (
            float((end_value - start_value) / start_value * 100) if start_value > 0 else None
        )
        pl_pct = float((end_value - end_cost) / end_cost * 100) if end_cost > 0 else None

        return EquityCurve(
            account_label=label,
            points=points,
            max_drawdown_pct=max_dd,
            period_return_pct=period_return,
            pl_pct=pl_pct,
            xirr=self._xirr(sorted_txns, window_start, start_value, today, end_value),
            unpriced=sorted(set(unpriced)),
        )

    @staticmethod
    def _xirr(
        sorted_txns: list[Transaction],
        window_start: date,
        start_value: Decimal,
        end: date,
        end_value: Decimal,
    ) -> float | None:
        """Money-weighted return over the window: opening holdings are an initial
        outflow, in-window trades are flows, closing value is the terminal inflow."""
        flows: list[tuple[date, float]] = []
        if start_value > 0:
            flows.append((window_start, -float(start_value)))
        for t in sorted_txns:
            d = t.executed_at.date()
            if d <= window_start or d > end:
                continue
            amount = float(t.quantity * t.price)
            if t.side.value == "buy":
                flows.append((d, -(amount + float(t.fees))))
            else:
                flows.append((d, amount - float(t.fees)))
        flows.append((end, float(end_value)))
        return xirr(flows)
