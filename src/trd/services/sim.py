from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError
from trd.models import Account, AccountType, Side, Transaction
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, InstrumentRepo, TransactionRepo, WatchlistRepo
from trd.services.portfolio import PortfolioService

BENCHMARK = "SPY"
MOMENTUM_BARS = 63  # ~3 months of trading days


class SimConfig(BaseModel):
    account: Account
    monthly_amount: Decimal
    strategy: str
    strategy_ticker: str | None = None
    allocations: dict[str, Decimal] = {}  # symbol -> weight in percent, sums to 100

    @property
    def strategy_label(self) -> str:
        if self.strategy == "ticker":
            return self.strategy_ticker or "—"
        if self.strategy == "allocation":
            return " / ".join(f"{w.normalize():f}% {s}" for s, w in self.allocations.items())
        return "momentum (watchlist)"


class SimStatus(BaseModel):
    config: SimConfig
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


class SimService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.txns = TransactionRepo(conn)
        self.watchlists = WatchlistRepo(conn)
        self.portfolio = PortfolioService(conn, provider)

    def init(
        self,
        monthly: Decimal,
        strategy: str = "ticker",
        ticker: str | None = "SPY",
        name: str = "sim",
        allocations: dict[str, Decimal] | None = None,
    ) -> SimConfig:
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
        existing = self.accounts.get_by_name(name)
        if existing is not None and self._config_row(existing.id) is not None:
            raise TrdError(f"Simulation account '{name}' already exists.")
        account = existing or self.accounts.create(name, AccountType.SIMULATION)
        self.conn.execute(
            """
            INSERT INTO sim_config (account_id, monthly_amount, strategy, strategy_ticker)
            VALUES (?, ?, ?, ?)
            """,
            [
                account.id,
                monthly,
                strategy,
                ticker.upper() if ticker and strategy == "ticker" else None,
            ],
        )
        if allocations:
            for symbol, weight in allocations.items():
                self.conn.execute(
                    "INSERT INTO sim_allocation (account_id, symbol, weight) VALUES (?, ?, ?)",
                    [account.id, symbol.upper(), weight],
                )
        return self.get_config(name)

    def _config_row(self, account_id: int) -> tuple | None:
        return self.conn.execute(
            "SELECT monthly_amount, strategy, strategy_ticker FROM sim_config WHERE account_id = ?",
            [account_id],
        ).fetchone()

    def get_config(self, name: str = "sim") -> SimConfig:
        account = self.accounts.get_by_name(name)
        row = self._config_row(account.id) if account else None
        if account is None or row is None:
            raise TrdError(f"No simulation account '{name}'. Run 'trd sim init' first.")
        allocations = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT symbol, weight FROM sim_allocation WHERE account_id = ? "
                "ORDER BY weight DESC",
                [account.id],
            ).fetchall()
        }
        return SimConfig(
            account=account,
            monthly_amount=row[0],
            strategy=row[1],
            strategy_ticker=row[2],
            allocations=allocations,
        )

    def _sim_txns(self, account_id: int) -> list[Transaction]:
        return self.txns.list_chronological(account_id)

    def invest(self, name: str = "sim", when: date | None = None) -> list[Transaction]:
        """Execute one month's contribution (one buy, or several for 'allocation').
        Backdate with `when` to build history."""
        config = self.get_config(name)
        target = when or date.today()
        month = (target.year, target.month)
        for txn in self._sim_txns(config.account.id):
            if (txn.executed_at.year, txn.executed_at.month) == month:
                raise TrdError(
                    f"Already invested for {target.year}-{target.month:02d} "
                    f"({txn.executed_at.date()})."
                )
        if config.strategy == "allocation":
            buys = [
                (symbol, config.monthly_amount * weight / 100)
                for symbol, weight in config.allocations.items()
            ]
        else:
            buys = [(self._pick_symbol(config, target), config.monthly_amount)]
        txns: list[Transaction] = []
        for symbol, amount in buys:
            price = self._price_for(symbol, target, live_ok=when is None)
            quantity = (amount / price).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            txns.append(
                self.portfolio.record_trade(
                    account_name=name,
                    symbol=symbol,
                    side=Side.BUY,
                    quantity=quantity,
                    price=price,
                    executed_at=datetime(target.year, target.month, target.day, 16, 0),
                    note=f"sim monthly invest ({config.strategy})",
                )
            )
        return txns

    def _pick_symbol(self, config: SimConfig, target: date) -> str:
        if config.strategy == "ticker":
            assert config.strategy_ticker is not None
            return config.strategy_ticker
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
        row = self.conn.execute(
            """
            SELECT close FROM price_daily
            WHERE instrument_id = ? AND date BETWEEN ? AND ?
            ORDER BY date LIMIT 1
            """,
            [instrument.id, target, target + timedelta(days=7)],
        ).fetchone()
        if row is None:
            raise TrdError(
                f"No price history for {symbol} near {target}. Run 'trd sync --full' first."
            )
        return row[0]

    def status(self, name: str = "sim") -> SimStatus:
        config = self.get_config(name)
        txns = self._sim_txns(config.account.id)
        invested = sum((t.quantity * t.price + t.fees for t in txns), Decimal(0))
        positions = self.portfolio.positions(name)
        values = [p.market_value for p in positions]
        value = None if any(v is None for v in values) else sum(values, Decimal(0))
        benchmark_value = self._benchmark_value(txns)
        months = {(t.executed_at.year, t.executed_at.month) for t in txns}
        return SimStatus(
            config=config,
            months_invested=len(months),
            invested=invested,
            value=value,
            benchmark_value=benchmark_value,
        )

    def _benchmark_value(self, txns: list[Transaction]) -> Decimal | None:
        """What the same contributions would be worth in SPY, bought the same days."""
        if not txns:
            return None
        benchmark = self.instruments.get_by_symbol(BENCHMARK)
        if benchmark is None:
            return None
        shares = Decimal(0)
        for txn in txns:
            when = txn.executed_at.date()
            row = self.conn.execute(
                """
                SELECT close FROM price_daily
                WHERE instrument_id = ? AND date BETWEEN ? AND ?
                ORDER BY date LIMIT 1
                """,
                [benchmark.id, when, when + timedelta(days=7)],
            ).fetchone()
            if row is None or row[0] == 0:
                return None
            shares += (txn.quantity * txn.price + txn.fees) / row[0]
        latest = self.conn.execute(
            "SELECT close FROM price_daily WHERE instrument_id = ? ORDER BY date DESC LIMIT 1",
            [benchmark.id],
        ).fetchone()
        if latest is None:
            return None
        return shares * latest[0]
