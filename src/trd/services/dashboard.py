"""The home dashboard: an executive summary across real accounts answering the
six questions every everyday investor has — how much do I have, how much did I
put in, how much have I made, am I doing well, would I have beaten the market,
am I taking too much risk. Pure composition of PortfolioService + xirr +
same-dates benchmark; money is Decimal, return metrics are floats."""

from datetime import date
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.models import Position
from trd.providers.base import MarketDataProvider
from trd.services.benchmark import BENCHMARK, same_dates_value
from trd.services.portfolio import PortfolioService
from trd.services.xirr import xirr

CONCENTRATION_WARN = Decimal(25)  # top holding ≥ this % flags concentration risk


class Holding(BaseModel):
    symbol: str
    value: Decimal
    weight: Decimal  # percent of portfolio value
    pl: Decimal | None
    pl_pct: Decimal | None


class Dashboard(BaseModel):
    value: Decimal
    invested: Decimal  # cost basis of current holdings
    gains: Decimal
    today_change: Decimal
    holdings: list[Holding]  # sorted by value desc
    winners: list[Holding]  # best by pl_pct, desc
    losers: list[Holding]  # worst by pl_pct, asc
    positions_up: int
    positions_down: int
    xirr: float | None
    benchmark_value: Decimal | None
    spy_today_pct: Decimal | None

    @property
    def total_return_pct(self) -> Decimal | None:
        if self.invested == 0:
            return None
        return self.gains / self.invested * 100

    @property
    def today_change_pct(self) -> Decimal | None:
        prior = self.value - self.today_change
        if prior == 0:
            return None
        return self.today_change / prior * 100

    @property
    def top_holding(self) -> Holding | None:
        return self.holdings[0] if self.holdings else None

    @property
    def top5_weight(self) -> Decimal:
        return sum((h.weight for h in self.holdings[:5]), Decimal(0))

    @property
    def concentration_warning(self) -> bool:
        top = self.top_holding
        return top is not None and top.weight >= CONCENTRATION_WARN

    @property
    def benchmark_return_pct(self) -> Decimal | None:
        if self.benchmark_value is None or self.invested == 0:
            return None
        return (self.benchmark_value - self.invested) / self.invested * 100

    @property
    def alpha(self) -> Decimal | None:
        """Total return minus what the same money in the S&P 500 would have returned."""
        mine, theirs = self.total_return_pct, self.benchmark_return_pct
        if mine is None or theirs is None:
            return None
        return mine - theirs

    @property
    def win_rate(self) -> Decimal | None:
        total = self.positions_up + self.positions_down
        if total == 0:
            return None
        return Decimal(self.positions_up) / Decimal(total) * 100


class DashboardService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.portfolio = PortfolioService(conn, provider)

    def summary(self, include_simulation: bool = False) -> Dashboard:
        positions = self.portfolio.positions(include_simulation=include_simulation)

        value = sum((p.market_value for p in positions if p.market_value is not None), Decimal(0))
        invested = sum((p.cost_basis for p in positions), Decimal(0))
        gains = sum((p.unrealized_pl for p in positions if p.unrealized_pl is not None), Decimal(0))
        today_change = sum(
            (p.day_change for p in positions if p.day_change is not None), Decimal(0)
        )

        holdings = self._holdings(positions, value)
        ranked = [h for h in holdings if h.pl_pct is not None]

        def by_return(h: Holding) -> Decimal:
            assert h.pl_pct is not None
            return h.pl_pct

        winners = sorted(ranked, key=by_return, reverse=True)[:5]
        losers = sorted(ranked, key=by_return)[:5]
        up = sum(1 for p in positions if p.unrealized_pl is not None and p.unrealized_pl > 0)
        down = sum(1 for p in positions if p.unrealized_pl is not None and p.unrealized_pl < 0)

        return Dashboard(
            value=value,
            invested=invested,
            gains=gains,
            today_change=today_change,
            holdings=holdings,
            winners=winners,
            losers=losers,
            positions_up=up,
            positions_down=down,
            xirr=self._portfolio_xirr(positions, value, include_simulation),
            benchmark_value=self._benchmark(include_simulation),
            spy_today_pct=self._spy_today(),
        )

    def _holdings(self, positions: list[Position], total_value: Decimal) -> list[Holding]:
        holdings = [
            Holding(
                symbol=p.instrument.symbol,
                value=p.market_value or Decimal(0),
                weight=(p.market_value / total_value * 100)
                if p.market_value is not None and total_value
                else Decimal(0),
                pl=p.unrealized_pl,
                pl_pct=p.unrealized_pl_pct,
            )
            for p in positions
        ]
        holdings.sort(key=lambda h: h.value, reverse=True)
        return holdings

    def _portfolio_xirr(
        self, positions: list[Position], value: Decimal, include_simulation: bool
    ) -> float | None:
        if value == 0:
            return None
        flows: list[tuple[date, float]] = []
        for txns in self.portfolio._scope_txns(None, include_simulation).values():
            for txn in txns:
                amount = float(txn.quantity * txn.price + txn.fees)
                flows.append((txn.executed_at.date(), -amount if txn.side == "buy" else amount))
        if not flows:
            return None
        flows.append((date.today(), float(value)))
        return xirr(flows)

    def _benchmark(self, include_simulation: bool) -> Decimal | None:
        benchmark = self.portfolio.instruments.get_by_symbol(BENCHMARK)
        if benchmark is None:
            return None
        txns = [
            txn
            for txns in self.portfolio._scope_txns(None, include_simulation).values()
            for txn in txns
            if txn.side == "buy"
        ]
        return same_dates_value(self.portfolio.prices, benchmark.id, txns)

    def _spy_today(self) -> Decimal | None:
        quotes = self.provider.get_quotes([BENCHMARK])
        quote = quotes.get(BENCHMARK)
        return quote.day_change_pct if quote else None
