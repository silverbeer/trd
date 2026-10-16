"""Entry edge: do the engine's entries beat a random entry in the same name?

`trd engine outcomes` says what the taken signals did next — on the day engine,
an average best of +1.04R against an average worst of -1.05R. Symmetric paths
are what a coin flip looks like, but "looks like" is an opinion. This measures it.

For every signal the engine acted on, draw K random entries in the same symbol
on the same session (on a swing engine, within a few sessions either side),
restricted to the bars where the engine could actually have entered — after the
opening delay, before the entry cutoff. Each random entry is priced with the
stop `plan_entry` would have set at that bar and walked with the very function
that measures the real signal, `signal_outcome`. Taken and random are therefore
one unit, measured one way: the only thing that differs is *when* the entry
happened, which is exactly the thing an entry rule claims to know.

The comparison is paired. Each taken signal is set against the mean of its own
K draws, so a strong day in a strong name lifts both sides equally and the
difference is what the rule added. The standard error is over those per-signal
differences.

Nothing here judges a strategy. A rule indistinguishable from random is a
finding with an n beside it; whether to drop it is a person's decision.
"""

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

import duckdb
from pydantic import BaseModel, computed_field

from trd.engine.bars import BarSource
from trd.errors import TrdError
from trd.models import Bar, EngineConfig, EngineSignal, SignalOutcome
from trd.repos import PriceRepo
from trd.repos.engine import EngineConfigRepo, EngineSignalRepo
from trd.services.engine import ENTRY_CUTOFF_MINUTES, entry_opens_at
from trd.services.outcomes import follow_through_bars, risk_series, signal_outcome
from trd.timeframes import SESSION_OPEN_MINUTE

DEFAULT_SAMPLES = 20
DEFAULT_SEED = 1344
# A swing engine has one bar per session, so "the same session" is the signal's
# own bar. Its null draws from this many sessions either side instead.
SWING_NULL_SESSIONS = 10
# The SB-1033 split: entries in the first ten minutes carry half the heat.
EARLY_ENTRY_MINUTE = SESSION_OPEN_MINUTE + 10
# A difference of this many standard errors is the bar for "distinguishable".
SIGNIFICANT_SE = 2.0


class Measure(BaseModel):
    """One measurement, taken vs its random null, paired per signal."""

    taken: float | None = None
    taken_se: float | None = None
    random: float | None = None
    random_se: float | None = None
    edge: float | None = None
    edge_se: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def significant(self) -> bool:
        if self.edge is None or not self.edge_se:
            return False
        return abs(self.edge) > SIGNIFICANT_SE * self.edge_se


class EdgeGroup(BaseModel):
    """Taken signals in one slice — a strategy, a time of day, or all of them."""

    label: str
    signals: int
    mfe_r: Measure
    mae_r: Measure
    # MFE + MAE (MAE is negative): best offered minus worst suffered. Zero is a
    # symmetric path, which is what a random entry produces on average.
    net_r: Measure
    forward_r: Measure
    target_first_pct: Measure
    stop_first_pct: Measure


class EdgeReport(BaseModel):
    timeframe: str
    samples: int
    seed: int
    horizon_bars: int
    taken_signals: int
    measured: int
    # Taken signals with no eligible random bar or no stop to price — dropped from
    # both sides rather than compared against nothing.
    unmeasured: int
    overall: EdgeGroup
    by_strategy: list[EdgeGroup]
    by_time: list[EdgeGroup]
    caveats: list[str] = [
        "Paired per signal: each taken entry is compared with K random entries in the "
        "same name on the same session, walked by the same function over the same "
        "horizon. Edge = taken minus random; SE is over the per-signal differences.",
        "Random entries fill at their bar's close; a live signal fills at its quote "
        "inside the forming bar. Both walks start at the next bar.",
        "'Distinguishable' means |edge| > 2 SE. Not distinguishable is not proof of no "
        "edge — it is an edge too small for this n to see.",
    ]


@dataclass
class _Pair:
    """One taken signal's outcome beside the mean of its random draws."""

    strategy: str
    minute: int
    taken: SignalOutcome
    draws: list[SignalOutcome]


def _minute(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def eligible_bars(
    bars: Sequence[Bar],
    source: BarSource,
    session: date,
    params: dict[str, float],
    risk: dict[datetime, Decimal],
) -> list[int]:
    """Indexes of bars on `session` where the live engine could have entered.

    The same gates the scanner applies: no entry before the opening delay, none
    within the cutoff of a day engine's flat time, and none on a bar with no stop
    to size from.
    """
    opens = entry_opens_at(params) or SESSION_OPEN_MINUTE
    flat_at = int(params.get("flat_at_minute", 0))
    cutoff = (flat_at // 100) * 60 + flat_at % 100 - ENTRY_CUTOFF_MINUTES if flat_at else None
    out = []
    for i, bar in enumerate(bars):
        stamp = source.stamp(bar)
        if stamp.date() != session or stamp not in risk:
            continue
        minute = _minute(stamp)
        if minute < opens or (cutoff is not None and minute >= cutoff):
            continue
        out.append(i)
    return out


def _swing_pool(bars: Sequence[Bar], source: BarSource, at: int, risk: dict) -> list[int]:
    lo, hi = max(0, at - SWING_NULL_SESSIONS), min(len(bars), at + SWING_NULL_SESSIONS + 1)
    return [i for i in range(lo, hi) if i != at and source.stamp(bars[i]) in risk]


def random_draws(
    signal: EngineSignal,
    bars: Sequence[Bar],
    source: BarSource,
    params: dict[str, float],
    risk: dict[datetime, Decimal],
    horizon: int,
    samples: int,
    rng: random.Random,
) -> list[SignalOutcome]:
    """K random entries standing in for this signal, each measured exactly as it is.

    Each draw is the signal itself moved to another bar — same symbol, same
    strategy label — so `signal_outcome` cannot tell a draw from the real thing.
    """
    stamps = [source.stamp(b) for b in bars]
    if source.is_intraday:
        pool = eligible_bars(bars, source, signal.bar_ts.date(), params, risk)
    else:
        at = stamps.index(signal.bar_ts) if signal.bar_ts in stamps else None
        pool = [] if at is None else _swing_pool(bars, source, at, risk)
    if not pool:
        return []
    out = []
    for i in (rng.choice(pool) for _ in range(samples)):
        moved = signal.model_copy(update={"bar_ts": stamps[i], "price": bars[i].close})
        # The walk only reads bars after the entry; handing it the slice keeps
        # thousands of draws from rescanning the whole series each.
        outcome = signal_outcome(
            moved, bars[i : i + horizon + 1], source, risk[stamps[i]], horizon, 0, 1
        )
        if outcome is not None:
            out.append(outcome)
    return out


def _mean_se(values: list[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return mean, math.sqrt(var / len(values))


def _measure(pairs: list[_Pair], read) -> Measure:
    """Paired comparison of one reading. `read` maps an outcome to a float or None."""
    taken, null, diff = [], [], []
    for pair in pairs:
        t = read(pair.taken)
        draws = [v for v in (read(d) for d in pair.draws) if v is not None]
        if t is None or not draws:
            continue
        r = sum(draws) / len(draws)
        taken.append(t)
        null.append(r)
        diff.append(t - r)
    t_mean, t_se = _mean_se(taken)
    r_mean, r_se = _mean_se(null)
    d_mean, d_se = _mean_se(diff)
    return Measure(
        taken=t_mean, taken_se=t_se, random=r_mean, random_se=r_se, edge=d_mean, edge_se=d_se
    )


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


def _net(o: SignalOutcome) -> float | None:
    if o.mfe_r is None or o.mae_r is None:
        return None
    return float(o.mfe_r + o.mae_r)


def _first(kind: str):
    def read(o: SignalOutcome) -> float | None:
        if o.resolution is None:
            return None
        return 100.0 if o.resolution == kind else 0.0

    return read


def summarize(label: str, pairs: list[_Pair]) -> EdgeGroup:
    return EdgeGroup(
        label=label,
        signals=len(pairs),
        mfe_r=_measure(pairs, lambda o: _f(o.mfe_r)),
        mae_r=_measure(pairs, lambda o: _f(o.mae_r)),
        net_r=_measure(pairs, _net),
        forward_r=_measure(pairs, lambda o: _f(o.forward_r)),
        target_first_pct=_measure(pairs, _first("target")),
        stop_first_pct=_measure(pairs, _first("stop")),
    )


def measure_edge(
    signals: Sequence[EngineSignal],
    bars_for,
    source: BarSource,
    params: dict[str, float],
    samples: int = DEFAULT_SAMPLES,
    seed: int = DEFAULT_SEED,
) -> tuple[list[_Pair], int]:
    """Pair every taken signal with its random draws. Pure: `bars_for` maps an
    instrument id to its stored series. Returns (pairs, unmeasured)."""
    if samples <= 0:
        raise TrdError("Samples must be positive.")
    horizon = follow_through_bars(source.timeframe)
    rng = random.Random(seed)
    risks: dict[int, dict[datetime, Decimal]] = {}
    pairs: list[_Pair] = []
    unmeasured = 0
    # Ordered so the seed means the same draws on every run, whatever order the
    # repository happened to return.
    for signal in sorted(signals, key=lambda s: (s.fired_at, s.id)):
        bars = bars_for(signal.instrument_id)
        if signal.instrument_id not in risks:
            risks[signal.instrument_id] = risk_series(bars, source, params)
        risk = risks[signal.instrument_id]
        stamps = {source.stamp(b): i for i, b in enumerate(bars)}
        at = stamps.get(signal.bar_ts)
        taken = (
            signal_outcome(
                signal, bars[at : at + horizon + 1], source, risk.get(signal.bar_ts), horizon, 0, 1
            )
            if at is not None
            else None
        )
        draws = random_draws(signal, bars, source, params, risk, horizon, samples, rng)
        if taken is None or not draws:
            unmeasured += 1
            continue
        pairs.append(_Pair(signal.strategy, _minute(signal.bar_ts), taken, draws))
    return pairs, unmeasured


class EdgeService:
    """Loads the taken signals and their bars, then runs the pure comparison.
    Reads only — the CLI hands it a read-only connection."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.configs = EngineConfigRepo(conn)
        self.signals = EngineSignalRepo(conn)
        self.prices = PriceRepo(conn)

    def config(self) -> EngineConfig:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")
        return config

    def report(self, samples: int = DEFAULT_SAMPLES, seed: int = DEFAULT_SEED) -> EdgeReport:
        config = self.config()
        source = BarSource(self.prices, config.timeframe)
        taken = [s for s, _ in self.signals.list_recent(limit=10_000_000) if s.acted]
        if not taken:
            raise TrdError("No taken signals yet. Run 'trd engine scan' for a while first.")
        series: dict[int, list[Bar]] = {}

        def bars_for(instrument_id: int) -> list[Bar]:
            if instrument_id not in series:
                series[instrument_id] = source.stored(instrument_id)
            return series[instrument_id]

        pairs, unmeasured = measure_edge(
            taken, bars_for, source, dict(config.exit_params), samples, seed
        )
        if not pairs:
            raise TrdError("No taken signal could be measured — are the bars synced?")
        strategies = sorted({p.strategy for p in pairs})
        by_time: list[EdgeGroup] = []
        if source.is_intraday:
            early = [p for p in pairs if p.minute < EARLY_ENTRY_MINUTE]
            late = [p for p in pairs if p.minute >= EARLY_ENTRY_MINUTE]
            by_time = [summarize("before 09:40", early), summarize("09:40 and later", late)]
        return EdgeReport(
            timeframe=config.timeframe,
            samples=samples,
            seed=seed,
            horizon_bars=follow_through_bars(config.timeframe),
            taken_signals=len(taken),
            measured=len(pairs),
            unmeasured=unmeasured,
            overall=summarize("all taken", pairs),
            by_strategy=[summarize(k, [p for p in pairs if p.strategy == k]) for k in strategies],
            by_time=by_time,
        )
