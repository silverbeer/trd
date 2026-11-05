"""Rotation: what a full book cost (SB-1537).

When every slot is taken the engine records the signal it could not act on and
moves on. Nothing ever priced the alternative — sell the coldest position it
held and take the new signal instead. This does, before anyone writes a rule
that would do it for real.

For every session with at least one signal passed on a full book, the
strongest such signal (highest score — one rotation a session, the most a rule
would ever do) is paired with the COLDEST ELIGIBLE position open at that
moment:

- eligible: past the indicator exit's grace period (a pullback entry is held
  through its own dip on purpose) and not within `NEAR_TARGET_R` of its target
  (selling a trade about to pay out is not rotation, it is a worse exit)
- coldest: lowest R at the decision bar's close

The swap happens at the close of the signal's bar, for both legs — one instant,
so the comparison is not one leg's intraday quote against the other's close.
Then, over the same follow-through horizon `trd engine outcomes` uses:

- the candidate is walked with the stop `plan_entry` would have set and the
  target `target_r` above it, and leaves at whichever is touched first, else at
  the horizon's last close (a bar spanning both is the stop — the pessimistic
  read, as in `outcomes.resolution`)
- the holding is what keeping it did: its real exit price when it closed inside
  the horizon (the cash then sits idle), else the horizon's last close

Both legs are returns on the same slot of capital, so the swap's edge is the
difference in PERCENT, minus a round-trip cost. Not R: the two legs carry
different stops, and one R of each is a different number of dollars.

Read-only and no network: every number is a walk over stored bars.

Caveats, printed with the numbers: the candidate's exit is a stop/target/
horizon approximation of the engine's five exit rules, not a replay of them;
a swap would also have changed what the NEXT full-book session looked like,
which a per-session pairing cannot see; and a swap is two fills, not one.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal

import duckdb
from pydantic import BaseModel, computed_field

from trd.engine.bars import BarSource
from trd.errors import TrdError
from trd.models import Bar, EnginePosition, EngineSignal, Instrument
from trd.repos import PriceRepo
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo, EngineSignalRepo
from trd.services.outcomes import _bar_instant, follow_through_bars, open_at, risk_series
from trd.timeframes import sessions_to_bars

# A holding within this many R of its target is about to pay out; selling it to
# make room is a worse exit, not a rotation.
NEAR_TARGET_R = Decimal("0.5")
# A swap is two market orders. Commission-free, so this is spread and slippage
# on both legs, in percent of the slot. Deliberately pessimistic for liquid names.
ROUND_TRIP_COST_PCT = Decimal("0.10")
# Below this many measured swaps the result is a hypothesis, the bar the review uses.
HYPOTHESIS_BELOW = 20
SIGNIFICANT_SE = 2.0
DEFAULT_GRACE_SESSIONS = 3.0

CAVEATS = [
    "The candidate leaves at its stop, its target or the horizon — an approximation "
    "of the engine's five exit rules, not a replay of them.",
    "Each session is paired alone: a swap would also have changed which positions "
    "were open at the NEXT full-book session, and that cannot be seen from here.",
    "Both legs trade at the signal bar's close; a live swap is two fills at "
    "whatever the market gives.",
]


class Holding(BaseModel):
    """A position open at a decision instant, as a rotation sees it."""

    position_id: int
    symbol: str
    strategy: str
    sessions_held: int
    r_now: Decimal
    mark: Decimal


class RotationRow(BaseModel):
    """One full-book session: the strongest blocked signal vs the coldest holding."""

    session: date
    signal_id: int
    symbol: str
    strategy: str
    score: float
    entry: Decimal
    sold: Holding | None = None
    # None when there was no eligible holding, or when the horizon has not
    # filled in yet — `status` says which.
    candidate_pct: Decimal | None = None
    candidate_exit: str | None = None  # target | stop | horizon
    keep_pct: Decimal | None = None
    edge_pct: Decimal | None = None
    status: str  # measured | pending | no_eligible | no_stop


class Stat(BaseModel):
    """Mean swap edge with its standard error, over one slice of the rows."""

    label: str
    n: int
    mean_edge_pct: float | None = None
    se_pct: float | None = None
    # One volatile name can carry a mean; the median says whether the typical
    # swap helped. Measured 2026-11-04: five of the swing book's six largest
    # swap edges were the same low-priced name.
    median_edge_pct: float | None = None
    wins: int = 0
    mean_candidate_pct: float | None = None
    mean_keep_pct: float | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def significant(self) -> bool:
        if self.mean_edge_pct is None or not self.se_pct:
            return False
        return abs(self.mean_edge_pct) > SIGNIFICANT_SE * self.se_pct

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hypothesis(self) -> bool:
        return self.n < HYPOTHESIS_BELOW


class RotationReport(BaseModel):
    timeframe: str
    max_positions: int
    horizon_bars: int
    grace_sessions: float
    near_target_r: Decimal
    cost_pct: Decimal
    blocked_sessions: int
    overall: Stat
    by_strategy: list[Stat]
    halves: list[Stat]
    rows: list[RotationRow]
    caveats: list[str] = CAVEATS

    @computed_field  # type: ignore[prop-decorator]
    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows:
            out[row.status] = out.get(row.status, 0) + 1
        return out


# --------------------------------------------------------------- the arithmetic


def managed_return(
    bars: Sequence[Bar], entry: Decimal, risk: Decimal, target_r: Decimal
) -> tuple[Decimal, str] | None:
    """Return (as a fraction) of an entry at `entry` walked to its stop, its
    target or the last bar's close, whichever comes first."""
    if not bars or risk <= 0 or entry <= 0:
        return None
    stop = entry - risk
    target = entry + risk * target_r
    for bar in bars:
        if bar.low <= stop:
            return stop / entry - 1, "stop"
        if bar.high >= target:
            return target / entry - 1, "target"
    return bars[-1].close / entry - 1, "horizon"


def _index_at(bars: Sequence[Bar], source: BarSource, stamp: datetime) -> int | None:
    for i, bar in enumerate(bars):
        if source.stamp(bar) == stamp:
            return i
    return None


def holding_at(
    position: EnginePosition,
    instrument: Instrument,
    bars: Sequence[Bar],
    source: BarSource,
    stamp: datetime,
) -> tuple[Holding, int] | None:
    """The position as it stood at the close of the bar stamped `stamp`, and
    that bar's index. None when there is no bar to mark it at."""
    i = _index_at(bars, source, stamp)
    risk = position.risk_per_share
    if i is None or risk <= 0:
        return None
    opened = _bar_instant(position.opened_at, source)
    held = sum(1 for b in bars[: i + 1] if source.stamp(b) > opened)
    mark = bars[i].close
    return (
        Holding(
            position_id=position.id,
            symbol=instrument.symbol,
            strategy=position.strategy,
            sessions_held=held,
            r_now=(mark - position.entry_price) / risk,
            mark=mark,
        ),
        i,
    )


def coldest(holdings: Sequence[Holding], grace_bars: int, target_r: Decimal) -> Holding | None:
    """The holding a rotation would sell: past its grace, not near its target,
    lowest R. Shared with the rotation rule (SB-1539) so the measurement and the
    rule pick the same position."""
    eligible = [
        h for h in holdings if h.sessions_held >= grace_bars and h.r_now < target_r - NEAR_TARGET_R
    ]
    if not eligible:
        return None
    return min(eligible, key=lambda h: (h.r_now, h.position_id))


def keep_return(
    position: EnginePosition,
    bars: Sequence[Bar],
    source: BarSource,
    index: int,
    mark: Decimal,
    horizon: int,
) -> Decimal | None:
    """What keeping the holding from `mark` did over the horizon: its real exit
    when it closed inside it, else the horizon's last close. None while the
    horizon has not filled in."""
    ahead = list(bars[index + 1 : index + 1 + horizon])
    if position.closed_at is not None and position.exit_price is not None:
        closed = _bar_instant(position.closed_at, source)
        if not ahead or closed <= source.stamp(ahead[-1]):
            return position.exit_price / mark - 1
    if len(ahead) < horizon:
        return None
    return ahead[-1].close / mark - 1


def _stat(label: str, rows: list[RotationRow]) -> Stat:
    edges = [float(r.edge_pct) for r in rows if r.edge_pct is not None]
    n = len(edges)
    if not n:
        return Stat(label=label, n=0)
    mean = sum(edges) / n
    se = math.sqrt(sum((e - mean) ** 2 for e in edges) / (n - 1)) / math.sqrt(n) if n > 1 else None
    return Stat(
        label=label,
        n=n,
        mean_edge_pct=mean,
        se_pct=se,
        median_edge_pct=statistics.median(edges),
        wins=sum(1 for e in edges if e > 0),
        mean_candidate_pct=sum(float(r.candidate_pct or 0) for r in rows) / n,
        mean_keep_pct=sum(float(r.keep_pct or 0) for r in rows) / n,
    )


def summarize(rows: list[RotationRow]) -> tuple[Stat, list[Stat], list[Stat]]:
    measured = sorted((r for r in rows if r.status == "measured"), key=lambda r: r.session)
    overall = _stat("all", measured)
    strategies = sorted({r.strategy for r in measured})
    by_strategy = [_stat(s, [r for r in measured if r.strategy == s]) for s in strategies]
    half = len(measured) // 2
    halves = (
        [_stat("first half", measured[:half]), _stat("second half", measured[half:])]
        if half
        else []
    )
    return overall, by_strategy, halves


def measure_rotations(
    signals: Sequence[tuple[EngineSignal, Instrument]],
    positions: Sequence[tuple[EnginePosition, Instrument]],
    bars: Mapping[int, Sequence[Bar]],
    source: BarSource,
    exit_params: dict[str, float],
    max_positions: int,
    horizon: int,
    cost_pct: Decimal = ROUND_TRIP_COST_PCT,
) -> tuple[list[RotationRow], int]:
    """One row per session with a full-book signal. Returns (rows, blocked sessions).

    Pure: every input is handed in, so a test can build the answer by hand.
    """
    plain = [p for p, _ in positions]
    target_r = Decimal(str(exit_params.get("target_r", 2.0)))
    grace = exit_params.get("indicator_grace_sessions", DEFAULT_GRACE_SESSIONS)
    grace_bars = sessions_to_bars(source.timeframe, grace)

    # The strongest blocked signal per session: one rotation a session at most.
    best: dict[date, tuple[EngineSignal, Instrument]] = {}
    for signal, instrument in signals:
        if signal.acted or open_at(plain, signal.fired_at) < max_positions:
            continue
        day = signal.bar_ts.date()
        if day not in best or signal.score > best[day][0].score:
            best[day] = (signal, instrument)

    risks: dict[int, dict[datetime, Decimal]] = {}
    rows: list[RotationRow] = []
    for day in sorted(best):
        signal, instrument = best[day]
        series = bars.get(instrument.id, [])
        i = _index_at(series, source, signal.bar_ts)
        if instrument.id not in risks:
            risks[instrument.id] = risk_series(series, source, exit_params)
        risk = risks[instrument.id].get(signal.bar_ts)
        base = RotationRow(
            session=day,
            signal_id=signal.id,
            symbol=instrument.symbol,
            strategy=signal.strategy,
            score=signal.score,
            entry=signal.price,
            status="no_stop",
        )
        if i is None or risk is None:
            rows.append(base)
            continue
        entry = series[i].close

        held: list[tuple[Holding, EnginePosition, int]] = []
        for position, inst in positions:
            if inst.id == instrument.id or open_at([position], signal.fired_at) == 0:
                continue
            marked = holding_at(position, inst, bars.get(inst.id, []), source, signal.bar_ts)
            if marked is not None:
                held.append((marked[0], position, marked[1]))
        sold = coldest([h for h, _, _ in held], grace_bars, target_r)
        if sold is None:
            rows.append(base.model_copy(update={"entry": entry, "status": "no_eligible"}))
            continue
        picked = sold.position_id
        _, position, j = next(x for x in held if x[0].position_id == picked)
        sold_bars = bars.get(position.instrument_id, [])

        ahead = series[i + 1 : i + 1 + horizon]
        walked = managed_return(ahead, entry, risk, target_r) if len(ahead) == horizon else None
        keep = keep_return(position, sold_bars, source, j, sold.mark, horizon)
        if walked is None or keep is None:
            rows.append(base.model_copy(update={"entry": entry, "sold": sold, "status": "pending"}))
            continue
        cand, how = walked
        cand_pct, keep_pct = cand * 100, keep * 100
        rows.append(
            base.model_copy(
                update={
                    "entry": entry,
                    "sold": sold,
                    "candidate_pct": cand_pct,
                    "candidate_exit": how,
                    "keep_pct": keep_pct,
                    "edge_pct": cand_pct - keep_pct - cost_pct,
                    "status": "measured",
                }
            )
        )
    return rows, len(best)


# ------------------------------------------------------------------ the service


class RotationService:
    """Loads one engine home's signals, positions and bars and measures them."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.configs = EngineConfigRepo(conn)
        self.positions = EnginePositionRepo(conn)
        self.signals = EngineSignalRepo(conn)
        self.prices = PriceRepo(conn)

    def report(
        self,
        cost_pct: Decimal = ROUND_TRIP_COST_PCT,
        start: date | None = None,
        end: date | None = None,
    ) -> RotationReport:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")
        source = BarSource(self.prices, config.timeframe)
        horizon = follow_through_bars(config.timeframe)
        signals = [
            (s, i)
            for s, i in self.signals.list_recent(limit=1_000_000)
            if (start is None or s.bar_ts.date() >= start)
            and (end is None or s.bar_ts.date() <= end)
        ]
        positions = self.positions.list_all(config.account_id)
        ids = {i.id for _, i in signals} | {i.id for _, i in positions}
        bars = {iid: source.stored(iid) for iid in ids}
        rows, blocked = measure_rotations(
            signals,
            positions,
            bars,
            source,
            config.exit_params,
            config.max_positions,
            horizon,
            cost_pct,
        )
        overall, by_strategy, halves = summarize(rows)
        return RotationReport(
            timeframe=config.timeframe,
            max_positions=config.max_positions,
            horizon_bars=horizon,
            grace_sessions=config.exit_params.get(
                "indicator_grace_sessions", DEFAULT_GRACE_SESSIONS
            ),
            near_target_r=NEAR_TARGET_R,
            cost_pct=cost_pct,
            blocked_sessions=blocked,
            overall=overall,
            by_strategy=by_strategy,
            halves=halves,
            rows=rows,
        )
