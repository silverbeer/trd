"""DCA forecasting and backtesting. Floats throughout — projections are
signals, not ledger entries (same policy as indicators/math.py). Historical
math uses adjusted closes (dividends + splits) via PriceRepo helpers."""

import calendar
import random
from datetime import date, timedelta

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError
from trd.providers.base import MarketDataProvider
from trd.services.plan import BENCHMARK, Plan, PlanService
from trd.services.xirr import xirr

MIN_MONTHLY_RETURNS = 24


class YearBand(BaseModel):
    year: int
    contributed: float  # cumulative contributions through this year
    deterministic: float
    p10: float
    p50: float
    p90: float


class ForecastResult(BaseModel):
    monthly: float
    months: int
    monthly_growth: float  # geometric mean monthly return of the allocation
    cagr: float  # annualized
    window_start: date
    window_months: int
    limiting_symbol: str
    trials: int
    seed: int | None
    starting_value: float
    years: list[YearBand]


class BacktestResult(BaseModel):
    start: date
    end: date
    months: int
    skipped_months: int
    invested: float
    value: float
    xirr: float | None
    spy_value: float
    spy_xirr: float | None
    window_limited_by: str | None  # symbol that shortened the requested window

    @property
    def pl(self) -> float:
        return self.value - self.invested

    @property
    def pl_pct(self) -> float | None:
        if self.invested == 0:
            return None
        return self.pl / self.invested * 100

    @property
    def vs_spy(self) -> float:
        return self.value - self.spy_value


def _weights(plan: Plan) -> dict[str, float]:
    if plan.strategy == "allocation":
        return {s: float(w) / 100.0 for s, w in plan.allocations.items()}
    if plan.strategy == "ticker":
        assert plan.strategy_ticker is not None
        return {plan.strategy_ticker: 1.0}
    raise TrdError(
        "Momentum plans pick a different symbol each month — not a fixed basket, so "
        "forecast/backtest can't model them yet. (v1 limitation.)"
    )


class DcaProjectionService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.plans = PlanService(conn, provider)
        self.prices = self.plans.prices

    # ── shared: historical monthly returns of the allocation ────────────────

    def _monthly_series(self, symbol: str) -> dict[date, float]:
        instrument = self.plans.instruments.get_by_symbol(symbol)
        if instrument is None:
            raise TrdError(f"{symbol} is not tracked — buy/watch it, then trd sync --years 10.")
        return {month: float(close) for month, close in self.prices.monthly_closes(instrument.id)}

    def portfolio_monthly_returns(self, plan: Plan) -> tuple[list[float], date, str]:
        """Weighted monthly returns over the joint window where every plan symbol
        has data. Returns (returns, window_start, limiting_symbol)."""
        weights = _weights(plan)
        series = {symbol: self._monthly_series(symbol) for symbol in weights}
        firsts = {symbol: min(months) for symbol, months in series.items() if months}
        if len(firsts) < len(weights):
            missing = sorted(set(weights) - set(firsts))
            raise TrdError(
                f"No price history for {', '.join(missing)} — run 'trd sync --years 10'."
            )
        limiting_symbol = max(firsts, key=lambda s: firsts[s])
        window_start = firsts[limiting_symbol]
        common = sorted(set.intersection(*(set(m) for m in series.values())))
        common = [m for m in common if m >= window_start]

        returns: list[float] = []
        for prev, current in zip(common, common[1:], strict=False):
            r = 0.0
            for symbol, weight in weights.items():
                prev_close, close = series[symbol].get(prev), series[symbol].get(current)
                if prev_close is None or close is None or prev_close == 0:
                    break
                r += weight * (close / prev_close - 1.0)
            else:
                returns.append(r)
        if len(returns) < MIN_MONTHLY_RETURNS:
            raise TrdError(
                f"Only {len(returns)} monthly returns available (need {MIN_MONTHLY_RETURNS}). "
                "Run 'trd sync --years 10' for deeper history."
            )
        return returns, window_start, limiting_symbol

    # ── forecast ─────────────────────────────────────────────────────────────

    def forecast(
        self,
        account_name: str,
        years: int = 10,
        monthly_override: float | None = None,
        trials: int = 1000,
        seed: int | None = None,
    ) -> ForecastResult:
        plan = self.plans.get_plan(account_name)
        returns, window_start, limiting_symbol = self.portfolio_monthly_returns(plan)
        monthly = monthly_override if monthly_override is not None else float(plan.monthly_amount)
        months = years * 12

        product = 1.0
        for r in returns:
            product *= 1.0 + r
        g = product ** (1.0 / len(returns)) - 1.0

        status = self.plans.status(account_name)
        v0 = float(status.value) if status.value is not None else 0.0

        deterministic = self._deterministic_curve(v0, monthly, g, years)
        bands = self._bootstrap_bands(v0, monthly, returns, years, trials, seed)

        year_rows: list[YearBand] = []
        for year in range(1, years + 1):
            contributed = monthly * 12 * year
            p10, p50, p90 = bands[year - 1]
            year_rows.append(
                YearBand(
                    year=year,
                    contributed=contributed,
                    deterministic=deterministic[year - 1],
                    p10=p10,
                    p50=p50,
                    p90=p90,
                )
            )
        return ForecastResult(
            monthly=monthly,
            months=months,
            monthly_growth=g,
            cagr=(1.0 + g) ** 12 - 1.0,
            window_start=window_start,
            window_months=len(returns),
            limiting_symbol=limiting_symbol,
            trials=trials,
            seed=seed,
            starting_value=v0,
            years=year_rows,
        )

    @staticmethod
    def _deterministic_curve(v0: float, monthly: float, g: float, years: int) -> list[float]:
        """Annuity-due future value at each 12-month mark: contribute, then grow."""
        out: list[float] = []
        for year in range(1, years + 1):
            months = year * 12
            if g == 0:
                out.append(v0 + monthly * months)
            else:
                growth = (1.0 + g) ** months
                out.append(v0 * growth + monthly * (growth - 1.0) / g * (1.0 + g))
        return out

    @staticmethod
    def _bootstrap_bands(
        v0: float,
        monthly: float,
        pool: list[float],
        years: int,
        trials: int,
        seed: int | None,
    ) -> list[tuple[float, float, float]]:
        """iid bootstrap: each simulated month draws a random historical month."""
        rng = random.Random(seed)
        months = years * 12
        snapshots: list[list[float]] = [[] for _ in range(years)]
        size = len(pool)
        for _ in range(trials):
            value = v0
            for month in range(1, months + 1):
                value = (value + monthly) * (1.0 + pool[rng.randrange(size)])
                if month % 12 == 0:
                    snapshots[month // 12 - 1].append(value)
        bands: list[tuple[float, float, float]] = []
        for year_values in snapshots:
            ordered = sorted(year_values)
            n = len(ordered)
            bands.append(
                (
                    ordered[round(0.10 * (n - 1))],
                    ordered[round(0.50 * (n - 1))],
                    ordered[round(0.90 * (n - 1))],
                )
            )
        return bands

    # ── backtest ─────────────────────────────────────────────────────────────

    def backtest(self, account_name: str, years: int = 10) -> BacktestResult:
        """Replay the plan's exact monthly buys against real history.
        Pure simulation — writes nothing."""
        plan = self.plans.get_plan(account_name)
        weights = _weights(plan)
        # data window check (also raises for untracked/short history)
        _, window_start, limiting_symbol = self.portfolio_monthly_returns(plan)

        today = date.today()
        requested_start = today - timedelta(days=int(years * 365.25))
        start = max(requested_start, window_start)
        limited_by = limiting_symbol if start > requested_start else None

        monthly = float(plan.monthly_amount)
        day = plan.day_of_month or 15

        instruments = {symbol: self.plans.instruments.get_by_symbol(symbol) for symbol in weights}
        benchmark = self.plans.instruments.get_by_symbol(BENCHMARK)
        if benchmark is None:
            raise TrdError(f"{BENCHMARK} is not tracked — run 'trd sync --years 10'.")

        shares: dict[str, float] = dict.fromkeys(weights, 0.0)
        spy_shares = 0.0
        flows: list[tuple[date, float]] = []
        months = skipped = 0

        year, month = start.year, start.month
        while (year, month) <= (today.year, today.month):
            buy_date = date(year, month, min(day, calendar.monthrange(year, month)[1]))
            if start <= buy_date <= today:
                legs: dict[str, float] = {}
                for symbol, weight in weights.items():
                    instrument = instruments[symbol]
                    assert instrument is not None
                    close = self.prices.close_on_or_after(instrument.id, buy_date, adjusted=True)
                    if close is None:
                        break
                    legs[symbol] = float(close)
                spy_close = self.prices.close_on_or_after(benchmark.id, buy_date, adjusted=True)
                if len(legs) == len(weights) and spy_close is not None:
                    for symbol, weight in weights.items():
                        shares[symbol] += monthly * weight / legs[symbol]
                    spy_shares += monthly / float(spy_close)
                    flows.append((buy_date, -monthly))
                    months += 1
                else:
                    skipped += 1
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

        value = 0.0
        for symbol in weights:
            instrument = instruments[symbol]
            assert instrument is not None
            latest = self.prices.latest_close(instrument.id, adjusted=True)
            if latest is None:
                raise TrdError(f"No price history for {symbol}.")
            value += shares[symbol] * float(latest)
        spy_latest = self.prices.latest_close(benchmark.id, adjusted=True)
        assert spy_latest is not None
        spy_value = spy_shares * float(spy_latest)

        invested = monthly * months
        plan_flows = [*flows, (today, value)]
        spy_flows = [*flows, (today, spy_value)]
        return BacktestResult(
            start=start,
            end=today,
            months=months,
            skipped_months=skipped,
            invested=invested,
            value=value,
            xirr=xirr(plan_flows),
            spy_value=spy_value,
            spy_xirr=xirr(spy_flows),
            window_limited_by=limited_by,
        )
