from collections import defaultdict
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError
from trd.models import Account, AccountType, Side, Transaction
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo, TransactionRepo, WatchlistRepo
from trd.services.benchmark import BENCHMARK, same_dates_value
from trd.services.fifo import fifo_position
from trd.services.portfolio import PortfolioService

MOMENTUM_BARS = 63  # ~3 months of trading days

__all__ = ["BENCHMARK", "Plan", "PlanService", "PlanStatus"]


class Plan(BaseModel):
    id: int
    account: Account
    monthly_amount: Decimal
    strategy: str
    strategy_ticker: str | None = None
    allocations: dict[str, Decimal] = {}  # symbol -> weight in percent, sums to 100
    note: str | None = None  # the goal: why this plan exists
    day_of_month: int | None = None  # scheduled buy day (e.g. 15); None = no fixed day
    active: bool = True

    @property
    def strategy_label(self) -> str:
        if self.strategy == "ticker":
            return self.strategy_ticker or "—"
        if self.strategy == "allocation":
            return " / ".join(f"{w.normalize():f}% {s}" for s, w in self.allocations.items())
        return "momentum (watchlist)"

    @property
    def is_paper(self) -> bool:
        return self.account.type == AccountType.SIMULATION


class PlanStatus(BaseModel):
    plan: Plan
    months_invested: int
    invested: Decimal
    value: Decimal | None
    benchmark_value: Decimal | None

    @property
    def pl(self) -> Decimal | None:
        if self.value is None:
            return None
        return self.value - self.invested

    @property
    def pl_pct(self) -> Decimal | None:
        if self.value is None or self.invested == 0:
            return None
        return (self.value - self.invested) / self.invested * 100

    @property
    def vs_benchmark(self) -> Decimal | None:
        if self.value is None or self.benchmark_value is None:
            return None
        return self.value - self.benchmark_value


class PlanService:
    """Recurring monthly contributions on any account.

    Simulation accounts = paper strategy experiments. Real accounts = the user's
    actual monthly investing, executed at their broker and recorded here — trd
    never places orders. Plan transactions are tagged with plan_id so the plan's
    performance is scored independently of other holdings in the account.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.txns = TransactionRepo(conn)
        self.prices = PriceRepo(conn)
        self.watchlists = WatchlistRepo(conn)
        self.portfolio = PortfolioService(conn, provider)

    def set_plan(
        self,
        account_name: str,
        monthly: Decimal,
        strategy: str = "ticker",
        ticker: str | None = "SPY",
        allocations: dict[str, Decimal] | None = None,
        create_simulation: bool = False,
        note: str | None = None,
        day_of_month: int | None = None,
    ) -> Plan:
        if allocations:
            strategy = "allocation"
        if strategy not in ("ticker", "momentum", "allocation"):
            raise TrdError("Strategy must be 'ticker', 'momentum', or 'allocation'.")
        if strategy == "ticker" and not ticker:
            raise TrdError("The 'ticker' strategy needs --ticker.")
        if strategy == "allocation":
            if not allocations:
                raise TrdError("The 'allocation' strategy needs --alloc SYMBOL=WEIGHT entries.")
            total = sum(allocations.values(), Decimal(0))
            if total != 100:
                raise TrdError(f"Allocation weights must sum to 100, got {total.normalize():f}.")
        if monthly <= 0:
            raise TrdError("Monthly amount must be positive.")
        self._validate_day(day_of_month)
        account = self.accounts.get_by_name(account_name)
        if account is None:
            if not create_simulation:
                raise TrdError(
                    f"No account named '{account_name}'. Create it first (trd account add)."
                )
            account = self.accounts.create(account_name, AccountType.SIMULATION)
        if self._plan_row(account.id) is not None:
            raise TrdError(f"Account '{account_name}' already has a plan.")
        self.conn.execute(
            """
            INSERT INTO contribution_plan
                (account_id, monthly_amount, strategy, strategy_ticker, note, day_of_month)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                account.id,
                monthly,
                strategy,
                ticker.upper() if ticker and strategy == "ticker" else None,
                note,
                day_of_month,
            ],
        )
        plan = self.get_plan(account_name)
        if allocations:
            for symbol, weight in allocations.items():
                self.conn.execute(
                    "INSERT INTO plan_allocation (plan_id, symbol, weight) VALUES (?, ?, ?)",
                    [plan.id, symbol.upper(), weight],
                )
        return self.get_plan(account_name)

    @staticmethod
    def _validate_day(day_of_month: int | None) -> None:
        if day_of_month is not None and not 1 <= day_of_month <= 31:
            raise TrdError(f"--day must be 1-31, got {day_of_month}.")

    def _plan_row(self, account_id: int) -> tuple | None:
        return self.conn.execute(
            "SELECT id, monthly_amount, strategy, strategy_ticker, note, day_of_month, active "
            "FROM contribution_plan WHERE account_id = ?",
            [account_id],
        ).fetchone()

    def update_plan(
        self,
        account_name: str,
        monthly: Decimal | None = None,
        day_of_month: int | None = None,
        note: str | None = None,
    ) -> Plan:
        """Partial update of an existing plan (set_plan rejects duplicates)."""
        plan = self.get_plan(account_name)
        if monthly is not None and monthly <= 0:
            raise TrdError("Monthly amount must be positive.")
        self._validate_day(day_of_month)
        if monthly is None and day_of_month is None and note is None:
            raise TrdError("Nothing to update — pass --monthly, --day, or --note.")
        self.conn.execute(
            """
            UPDATE contribution_plan SET
                monthly_amount = coalesce(?, monthly_amount),
                day_of_month = coalesce(?, day_of_month),
                note = coalesce(?, note)
            WHERE id = ?
            """,
            [monthly, day_of_month, note, plan.id],
        )
        return self.get_plan(account_name)

    def pause(self, account_name: str) -> Plan:
        plan = self.get_plan(account_name)
        self.conn.execute("UPDATE contribution_plan SET active = false WHERE id = ?", [plan.id])
        return self.get_plan(account_name)

    def resume(self, account_name: str) -> Plan:
        plan = self.get_plan(account_name)
        self.conn.execute("UPDATE contribution_plan SET active = true WHERE id = ?", [plan.id])
        return self.get_plan(account_name)

    def get_plan(self, account_name: str) -> Plan:
        account = self.accounts.get_by_name(account_name)
        row = self._plan_row(account.id) if account else None
        if account is None or row is None:
            raise TrdError(
                f"No plan on account '{account_name}'. Run 'trd plan set' or 'trd sim init'."
            )
        allocations = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT symbol, weight FROM plan_allocation WHERE plan_id = ? ORDER BY weight DESC",
                [row[0]],
            ).fetchall()
        }
        return Plan(
            id=row[0],
            account=account,
            monthly_amount=row[1],
            strategy=row[2],
            strategy_ticker=row[3],
            allocations=allocations,
            note=row[4],
            day_of_month=row[5],
            active=row[6],
        )

    def list_plans(self) -> list[Plan]:
        names = [
            r[0]
            for r in self.conn.execute(
                """
                SELECT a.name FROM contribution_plan p
                JOIN account a ON a.id = p.account_id
                ORDER BY a.name
                """
            ).fetchall()
        ]
        return [self.get_plan(name) for name in names]

    def resolve_default_account(self) -> str:
        """When exactly one plan exists, commands can omit --account."""
        plans = self.list_plans()
        if len(plans) == 1:
            return plans[0].account.name
        if not plans:
            raise TrdError("No plans yet. Run 'trd plan set' or 'trd sim init'.")
        names = ", ".join(p.account.name for p in plans)
        raise TrdError(f"Multiple plans exist ({names}) — pass --account.")

    def invest(self, account_name: str, when: date | None = None) -> list[Transaction]:
        """Record one month's contribution (one buy, or several for 'allocation').

        For real accounts this RECORDS what was executed at the broker — trd
        never places orders. Backdate with `when` to use historical closes.
        """
        plan = self.get_plan(account_name)
        if not plan.active:
            raise TrdError(f"Plan on '{account_name}' is paused — trd dca resume.")
        target = when or date.today()
        month = (target.year, target.month)
        for txn in self.txns.list_for_plan(plan.id):
            if (txn.executed_at.year, txn.executed_at.month) == month:
                raise TrdError(
                    f"Plan on '{account_name}' already invested for "
                    f"{target.year}-{target.month:02d} ({txn.executed_at.date()})."
                )
        if plan.strategy == "allocation":
            buys = [
                (symbol, plan.monthly_amount * weight / 100)
                for symbol, weight in plan.allocations.items()
            ]
        else:
            buys = [(self._pick_symbol(plan, target), plan.monthly_amount)]
        txns: list[Transaction] = []
        for symbol, amount in buys:
            price = self._price_for(symbol, target, live_ok=when is None)
            quantity = (amount / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            txns.append(
                self.portfolio.record_trade(
                    account_name=account_name,
                    symbol=symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    price=price,
                    executed_at=datetime(target.year, target.month, target.day, 16, 0),
                    note=f"plan monthly invest ({plan.strategy})",
                    plan_id=plan.id,
                )
            )
        return txns

    def _pick_symbol(self, plan: Plan, target: date) -> str:
        if plan.strategy == "ticker":
            assert plan.strategy_ticker is not None
            return plan.strategy_ticker
        # momentum: best ~3-month return among watched instruments as of target date
        candidates = {inst.symbol: inst for _, inst in self.watchlists.items()}
        if not candidates:
            raise TrdError("Momentum strategy needs a non-empty watchlist (trd watch add ...).")
        best_symbol, best_return = None, None
        for symbol, instrument in candidates.items():
            rows = self.conn.execute(
                """
                SELECT close FROM price_daily
                WHERE instrument_id = ? AND date <= ?
                ORDER BY date DESC LIMIT ?
                """,
                [instrument.id, target, MOMENTUM_BARS],
            ).fetchall()
            if len(rows) < MOMENTUM_BARS:
                continue
            newest, oldest = rows[0][0], rows[-1][0]
            if oldest == 0:
                continue
            ret = (newest - oldest) / oldest
            if best_return is None or ret > best_return:
                best_symbol, best_return = symbol, ret
        if best_symbol is None:
            raise TrdError(
                f"No watchlist instrument has {MOMENTUM_BARS} bars of history. "
                "Run 'trd sync --full'."
            )
        return best_symbol

    def _price_for(self, symbol: str, target: date, live_ok: bool) -> Decimal:
        if live_ok:
            return self.provider.get_quote(symbol).price
        instrument = self.portfolio.ensure_instrument(symbol)
        close = self.prices.close_on_or_after(instrument.id, target)
        if close is None:
            raise TrdError(
                f"No price history for {symbol} near {target}. Run 'trd sync --full' first."
            )
        return close

    def status(self, account_name: str) -> PlanStatus:
        """The plan's own performance — only plan-tagged transactions count,
        so other holdings in the same (real) account don't pollute it."""
        plan = self.get_plan(account_name)
        txns = self.txns.list_for_plan(plan.id)
        invested = sum((t.quantity * t.price + t.fees for t in txns), Decimal(0))

        by_instrument: dict[int, list[Transaction]] = defaultdict(list)
        for txn in txns:
            by_instrument[txn.instrument_id].append(txn)
        value: Decimal | None = Decimal(0)
        symbols = {
            i: inst.symbol for i in by_instrument if (inst := self.instruments.get(i)) is not None
        }
        quotes = self.provider.get_quotes(list(symbols.values()))
        for instrument_id, instrument_txns in by_instrument.items():
            quantity, _ = fifo_position(instrument_txns)
            if quantity == 0:
                continue
            quote = quotes.get(symbols.get(instrument_id, ""))
            if quote is None:
                value = None
                break
            value += quantity * quote.price

        months = {(t.executed_at.year, t.executed_at.month) for t in txns}
        return PlanStatus(
            plan=plan,
            months_invested=len(months),
            invested=invested,
            value=value,
            benchmark_value=self._benchmark_value(txns),
        )

    def _benchmark_value(self, txns: list[Transaction]) -> Decimal | None:
        """What the same contributions would be worth in SPY, bought the same days."""
        benchmark = self.instruments.get_by_symbol(BENCHMARK)
        if benchmark is None:
            return None
        return same_dates_value(self.prices, benchmark.id, txns)
