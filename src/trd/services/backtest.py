"""Backtest: replay the engine's rules against stored history.

The live engine has zero completed trades, and at ~39 per strategy per year it
would take four years before `trd engine report` could tell an edge from noise.
Replaying ten years of daily bars produces that sample in one run.

This is a *driver* around the same pure rules the live engine runs — the
strategy registry for entries, `evaluate_exits` for exits, `plan_entry` for
sizing. If this module ever needs its own copy of a rule, the design has gone
wrong. Nothing here touches the database or writes a row: positions are
in-memory `EnginePosition` objects, so all the P&L and R-multiple math is the
model's own.

What a daily bar cannot express, this module decides explicitly:

- Entries fill at the signal bar's close — the settled equivalent of the live
  engine acting intraday on the forming bar.
- Exits get two fidelities. Price-level rules (stop, trail, target) can be
  checked against the bar's open/low/high: a gap through the level fills at the
  open (the trade loses more than 1R, exactly the risk the earnings blackout
  exists to limit), an intrabar touch fills at the level, and when one bar
  touches both stop and target the stop wins — pessimistic, same order the live
  rules run in. Path-dependent rules (indicator, time) run once per bar at the
  close, on settled data. `--fill close` collapses everything to close-only.
- Day-mode configs (`flat_at_minute` set) are refused outright: daily bars have
  no clock, so a session-close engine would either silently backtest as a swing
  engine or produce zero trades. An explicit error beats a silent lie.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum

import duckdb
from pydantic import BaseModel

from trd.engine import REGISTRY as STRATEGIES
from trd.engine import evaluate_exits
from trd.engine.exits import RULES, ExitDecision
from trd.errors import TrdError
from trd.models import DailyBar, EnginePosition, PositionStatus, StrategyStat
from trd.repos import EarningsRepo, InstrumentRepo, PriceRepo, WatchlistRepo
from trd.repos.engine import EngineConfigRepo
from trd.services.engine import plan_entry, strategy_stats

CAVEAT = (
    "Backtests flatter: today's universe is the names that survived, fills pay no "
    "spread or slippage, entries fill at the signal bar's close, and stored prices "
    "are retroactively split-adjusted. Every number here is an upper bound, not a "
    "forecast."
)

# Rules that compare price to a level the position already carries. These are the
# only rules that can honestly be checked against a bar's open/low/high — OHLC
# says a level traded, but not what the indicators looked like when it did.
_LEVEL_RULES = ("stop", "trail", "target")

# Bars are daily; the exact clock only matters to session_close, which a backtest
# refuses to run. Noon for evaluation, the bell for fills.
_EVAL_TIME = time(12, 0)
_FILL_TIME = time(16, 0)


class FillMode(StrEnum):
    INTRABAR = "intrabar"
    CLOSE = "close"


class BacktestTrade(BaseModel):
    """One completed round trip, with both halves of its story."""

    symbol: str
    strategy: str
    entry_date: date
    entry_price: Decimal
    quantity: Decimal
    entry_reason: str
    exit_date: date
    exit_price: Decimal
    rule: str
    exit_reason: str
    pnl: Decimal
    r_multiple: Decimal | None = None


class EquityPoint(BaseModel):
    date: date
    value: Decimal


class BacktestResult(BaseModel):
    start: date
    end: date
    fill: FillMode
    earnings_blackout_days: int
    symbols: list[str]
    strategies: list[str]
    position_size: Decimal
    max_positions: int
    stats: list[StrategyStat]
    trades: list[BacktestTrade]
    open_at_end: int
    blackout_blocked: int
    equity: list[EquityPoint]
    start_value: Decimal
    end_value: Decimal
    max_drawdown_pct: float
    skipped: list[str] = []
    caveat: str = CAVEAT

    @property
    def total_return_pct(self) -> Decimal | None:
        if self.start_value == 0:
            return None
        return (self.end_value - self.start_value) / self.start_value * 100


def _level_decision(
    position: EnginePosition,
    bars: list[DailyBar],
    price: Decimal,
    params: dict[str, float],
    now: datetime,
) -> ExitDecision | None:
    """First price-level rule to fire at a probe price, in the live order
    (stop before trail before target), skipping the path-dependent rules."""
    for rule in RULES:
        if rule.key not in _LEVEL_RULES:
            continue
        decision = rule.check(position, bars, price, params, now)
        if decision is not None:
            return decision
    return None


def _level_price(
    decision: ExitDecision, position: EnginePosition, params: dict[str, float]
) -> Decimal:
    """Where the fill lands when a level was touched intrabar: at the level."""
    if decision.rule == "stop":
        return position.stop_price
    if decision.rule == "trail":
        mult = Decimal(str(params.get("trail_atr_mult", 3.0)))
        return position.trail_high - position.atr_at_entry * mult
    return position.target_price


def _check_exit(
    position: EnginePosition,
    bars: list[DailyBar],
    i: int,
    params: dict[str, float],
    now: datetime,
    fill: FillMode,
) -> tuple[Decimal, ExitDecision] | None:
    """Run one bar through the exit rules; return (fill price, decision) or None.

    Intrabar order is deliberately pessimistic: the open first (a gap through any
    level fills at the open, not at the level it jumped), then the low (stop and
    trail fill at their level), then the high (target fills at its level), then
    the close for the path-dependent rules. Level probes see only settled bars
    through yesterday and yesterday's trail high — nothing today's close knows.
    """
    bar = bars[i]
    if fill == FillMode.INTRABAR:
        prior = bars[:i]
        for probe_price in (bar.open, bar.low, bar.high):
            decision = _level_decision(position, prior, probe_price, params, now)
            if decision is not None:
                price = (
                    bar.open
                    if probe_price == bar.open
                    else _level_price(decision, position, params)
                )
                return price, decision
    # The close probe mirrors a live end-of-day scan: today's bar is settled and
    # the trail high has absorbed today's close before the rules read it.
    probe = position.model_copy(update={"trail_high": max(position.trail_high, bar.close)})
    decision = evaluate_exits(probe, bars[: i + 1], bar.close, params, now)
    if decision is not None:
        return bar.close, decision
    return None


def _in_blackout(earnings: list[date] | None, today: date, days: int) -> bool:
    """Same window as EngineService._earnings_blackout, checked in memory."""
    if not earnings or days <= 0:
        return False
    return any(0 <= (event - today).days <= days for event in earnings)


def simulate(
    bars_by_symbol: dict[str, list[DailyBar]],
    *,
    strategies: list[str],
    position_size: Decimal,
    max_positions: int,
    exit_params: dict[str, float],
    earnings_by_symbol: dict[str, list[date]] | None = None,
    earnings_blackout_days: int = 0,
    fill: FillMode = FillMode.INTRABAR,
    start: date | None = None,
    end: date | None = None,
) -> BacktestResult:
    """Walk history forward and run the live rules at every step.

    Dates walk the union of all symbols' bar dates — never per-symbol indexes,
    because two symbols' "bar 200" are different days and a per-symbol loop would
    silently break `max_positions`. At date *t* a strategy sees exactly the bars
    up to and including *t*; the prefix slice is the whole lookahead guarantee.
    `start` gates when trading may begin — earlier bars still feed the
    indicators as warmup.
    """
    if float(exit_params.get("flat_at_minute", 0)) > 0:
        raise TrdError(
            "Day-mode engines (flat_at_minute set) cannot be backtested on daily "
            "bars: the session-close exit needs an intraday clock the bars don't "
            "have. Backtest a swing configuration instead."
        )
    unknown = [k for k in strategies if k not in STRATEGIES]
    if unknown:
        raise TrdError(f"Unknown strategies: {', '.join(unknown)}")

    series = {
        symbol.upper(): [b for b in bars if end is None or b.date <= end]
        for symbol, bars in bars_by_symbol.items()
    }
    series = {s: bars for s, bars in series.items() if bars}
    if not series:
        raise TrdError("No price history to backtest. Run 'trd sync --years 10' first.")

    skipped: list[str] = []
    warmup = max(STRATEGIES[k].min_bars for k in strategies)
    for symbol in sorted(series):
        if len(series[symbol]) < warmup:
            skipped.append(
                f"{symbol}: only {len(series[symbol])} bars — some strategies need "
                f"{warmup} before they can fire (run 'trd sync --years 10')"
            )

    dates = sorted({bar.date for bars in series.values() for bar in bars})
    # Everything before `start` is warmup: the indicators see it, the report and
    # the equity curve begin where trading may.
    trading_start = max(start, dates[0]) if start is not None else dates[0]
    index = {s: {bar.date: i for i, bar in enumerate(bars)} for s, bars in series.items()}
    instrument_ids = {s: n for n, s in enumerate(sorted(series), start=1)}
    earnings = {s.upper(): dates_ for s, dates_ in (earnings_by_symbol or {}).items()}

    start_value = position_size * max_positions
    cash = start_value
    open_positions: dict[str, EnginePosition] = {}
    closed: list[EnginePosition] = []
    trades: list[BacktestTrade] = []
    entry_reasons: dict[int, str] = {}
    equity: list[EquityPoint] = []
    last_close: dict[str, Decimal] = {}
    blackout_blocked = 0
    next_id = 1

    for today in dates:
        now = datetime.combine(today, _EVAL_TIME)

        # Exits first, freeing capacity — the same order scan() uses. A position
        # whose symbol has no bar today simply rides.
        for symbol in sorted(open_positions):
            position = open_positions[symbol]
            i = index[symbol].get(today)
            if i is None:
                continue
            bars = series[symbol]
            bar = bars[i]
            position.bars_held += 1
            position.last_bar_date = today
            hit = _check_exit(position, bars, i, exit_params, now, fill)
            if hit is None:
                position.trail_high = max(position.trail_high, bar.close)
                continue
            exit_price, decision = hit
            position.status = PositionStatus.CLOSED
            position.closed_at = datetime.combine(today, _FILL_TIME)
            position.exit_price = exit_price
            position.exit_reason = decision.reason
            cash += exit_price * position.quantity
            closed.append(position)
            del open_positions[symbol]
            trades.append(
                BacktestTrade(
                    symbol=symbol,
                    strategy=position.strategy,
                    entry_date=position.opened_at.date(),
                    entry_price=position.entry_price,
                    quantity=position.quantity,
                    entry_reason=entry_reasons.get(position.id, ""),
                    exit_date=today,
                    exit_price=exit_price,
                    rule=decision.rule,
                    exit_reason=decision.reason,
                    pnl=position.realized_pnl or Decimal(0),
                    r_multiple=position.realized_r,
                )
            )

        # Entries: every enabled strategy over every eligible symbol, ranked the
        # way _run_entries ranks, filled while capacity lasts.
        capacity = max_positions - len(open_positions)
        if capacity > 0 and today >= trading_start:
            candidates: list[tuple[float, str, str, list[DailyBar], str]] = []
            for symbol in sorted(series):
                i = index[symbol].get(today)
                if i is None or symbol in open_positions:
                    continue
                prefix = series[symbol][: i + 1]
                blocked = _in_blackout(earnings.get(symbol), today, earnings_blackout_days)
                for key in strategies:
                    strategy = STRATEGIES[key]
                    if len(prefix) < strategy.min_bars:
                        continue
                    signal = strategy.evaluate(prefix)
                    if signal is None:
                        continue
                    if blocked:
                        blackout_blocked += 1
                        continue
                    candidates.append((signal.score, symbol, key, prefix, signal.reason))
            candidates.sort(key=lambda c: (-c[0], c[1]))
            for _score, symbol, key, prefix, reason in candidates:
                if capacity <= 0:
                    break
                if symbol in open_positions:
                    continue
                plan, _skip = plan_entry(prefix, position_size, exit_params)
                if plan is None:
                    continue
                price = prefix[-1].close
                position = EnginePosition(
                    id=next_id,
                    account_id=0,
                    instrument_id=instrument_ids[symbol],
                    strategy=key,
                    opened_at=datetime.combine(today, _FILL_TIME),
                    entry_price=price,
                    quantity=plan.quantity,
                    stop_price=plan.stop_price,
                    target_price=plan.target_price,
                    atr_at_entry=plan.atr,
                    trail_high=price,
                    last_bar_date=today,
                )
                entry_reasons[next_id] = reason
                next_id += 1
                open_positions[symbol] = position
                cash -= price * plan.quantity
                capacity -= 1

        for symbol, positions_index in index.items():
            i = positions_index.get(today)
            if i is not None:
                last_close[symbol] = series[symbol][i].close
        if today >= trading_start:
            value = cash + sum(
                (open_positions[s].quantity * last_close[s] for s in open_positions), Decimal(0)
            )
            equity.append(EquityPoint(date=today, value=value))

    open_counts: dict[str, int] = {}
    for position in open_positions.values():
        open_counts[position.strategy] = open_counts.get(position.strategy, 0) + 1

    peak = start_value
    max_drawdown = 0.0
    for point in equity:
        peak = max(peak, point.value)
        if peak > 0:
            max_drawdown = min(max_drawdown, float((point.value - peak) / peak * 100))

    return BacktestResult(
        start=trading_start,
        end=dates[-1],
        fill=fill,
        earnings_blackout_days=earnings_blackout_days,
        symbols=sorted(series),
        strategies=list(strategies),
        position_size=position_size,
        max_positions=max_positions,
        stats=strategy_stats(closed, open_counts),
        trades=trades,
        open_at_end=len(open_positions),
        blackout_blocked=blackout_blocked,
        equity=equity,
        start_value=start_value,
        end_value=equity[-1].value if equity else start_value,
        max_drawdown_pct=max_drawdown,
        skipped=skipped,
    )


class BacktestService:
    """Loads the configured engine's universe and history, then runs the pure
    simulation. The only service method that should ever grow here is loading —
    the decisions all live in `simulate`."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.configs = EngineConfigRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)
        self.watchlists = WatchlistRepo(conn)
        self.earnings = EarningsRepo(conn)

    def run(
        self,
        years: int | None = None,
        start: date | None = None,
        end: date | None = None,
        fill: FillMode = FillMode.INTRABAR,
        blackout: bool = True,
        symbols: list[str] | None = None,
    ) -> BacktestResult:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")

        if symbols is None:
            board = self.watchlists.get_by_name(config.watchlist)
            if board is None:
                raise TrdError(f"Engine watchlist '{config.watchlist}' is missing.")
            universe = [instrument.symbol for _, instrument in self.watchlists.items(board.id)]
        else:
            universe = [s.strip().upper() for s in symbols if s.strip()]
        if not universe:
            raise TrdError("Nothing to backtest — the universe is empty.")

        if years is not None:
            if years <= 0:
                raise TrdError("Years must be positive.")
            start = date.today() - timedelta(days=round(365.25 * years))

        bars_by_symbol: dict[str, list[DailyBar]] = {}
        earnings_by_symbol: dict[str, list[date]] = {}
        for symbol in universe:
            instrument = self.instruments.get_by_symbol(symbol)
            if instrument is None:
                raise TrdError(f"Unknown symbol {symbol} — 'trd quote {symbol}' adds it.")
            bars = self.prices.daily_bars(instrument.id)
            if not bars:
                raise TrdError(f"{symbol} has no price history. Run 'trd sync --years 10' first.")
            bars_by_symbol[symbol] = bars
            earnings_by_symbol[symbol] = self.earnings.dates_for_instrument(instrument.id)

        return simulate(
            bars_by_symbol,
            strategies=config.strategies,
            position_size=config.position_size,
            max_positions=config.max_positions,
            exit_params=config.exit_params,
            earnings_by_symbol=earnings_by_symbol,
            earnings_blackout_days=config.earnings_blackout_days if blackout else 0,
            fill=fill,
            start=start,
            end=end,
        )
