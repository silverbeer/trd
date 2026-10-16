"""Exit what-if: replay different exit rules on the entries the engine really took.

The backtest answers "what would this rule set have done" by simulating entries
too, and on the day engine its entries do not match the live ones — over the same
thirty days the two books came out 0.12R apart on 220 trades each (SB-1342). An
exit sweep run there measures the simulator as much as the exits.

This takes the entry question off the table. Every closed live trade keeps its
real fill price, size, ATR and entry instant; only the exit rules are re-run,
bar by bar, over the bars stored after the entry — once with the live config
(the *baseline* replay) and once per variant. The comparison that matters is
variant minus baseline on the SAME trade, which is paired: the trade-to-trade
noise cancels, and a difference of a few hundredths of an R becomes measurable
where an unpaired sweep could not see a tenth.

Three numbers, kept apart on purpose:

- **actual** — what the live trades booked. The reference, never the comparison:
  a live fill and a bar-close replay differ for reasons no exit param changes.
- **baseline** — the live params replayed. Its distance from *actual* is the
  replay's own error, and it is printed so nobody reads past it.
- **variant** — one param changed. Scored against baseline, paired.

Everything is in units of the LIVE trade's R (entry minus its real initial
stop). A variant that widens the stop has a bigger R of its own, and scoring it
in that unit would shrink every number it produces; one unit across rows is what
makes a row comparable at all. Sizing is by exposure, so the dollars move with
it.

The walk is `backtest.step_exit` — the same function `simulate` runs — so a
what-if row and a backtest row are one exit model with different entries.
Read-only: nothing here writes.
"""

from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from math import sqrt

import duckdb
from pydantic import BaseModel, computed_field

from trd.engine.bars import BarSource, bucket_start
from trd.engine.exits import DEFAULT_EXIT_PARAMS
from trd.errors import TrdError
from trd.models import Bar, EngineConfig, EnginePosition, PositionStatus
from trd.repos import InstrumentRepo, PriceRepo
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo
from trd.services.backtest import _EVAL_TIME, FillMode, default_fill, step_exit
from trd.services.engine import stop_distance
from trd.services.outcomes import excursion, pooled_capture, window

# Knobs that decide which trades exist, not how they end. A what-if holds the
# entries fixed, so varying one of these would report the baseline under a new
# name — refused rather than silently ignored.
ENTRY_PARAMS = frozenset({"entry_delay_minutes", "regime_sma", "regime_vix_max"})

# A paired difference this many standard errors from zero is the bar SB-1033
# set for touching the live config. Below it the row is noise, whatever its sign.
SIGNIFICANCE_SE = 2.0


class ReplayedExit(BaseModel):
    """One live entry walked forward under one set of exit params."""

    closed_at: datetime
    exit_price: Decimal
    rule: str
    # In units of the live trade's R, so every row of a sweep shares a scale.
    r: Decimal
    mfe_r: Decimal | None = None


class WhatIfArm(BaseModel):
    """One column of the comparison: a set of exit params over the same trades."""

    label: str
    params: dict[str, float] = {}
    trades: int
    expectancy_r: float | None = None
    se_r: float | None = None
    capture: Decimal | None = None
    win_rate: float | None = None
    rules: dict[str, int] = {}
    # Paired against baseline. None on the baseline and actual arms themselves.
    diff_r: float | None = None
    diff_se: float | None = None
    changed: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def diff_t(self) -> float | None:
        if self.diff_r is None or not self.diff_se:
            return None
        return self.diff_r / self.diff_se

    @computed_field  # type: ignore[prop-decorator]
    @property
    def significant(self) -> bool | None:
        """Whether the paired difference clears SIGNIFICANCE_SE. Says nothing
        about direction — read diff_r for that."""
        t = self.diff_t
        return None if t is None else abs(t) >= SIGNIFICANCE_SE


class WhatIfResult(BaseModel):
    timeframe: str
    fill: FillMode
    start: date | None = None
    end: date | None = None
    trades: int
    # Live trades left out, with why. Counted, never silently dropped.
    excluded: dict[str, int] = {}
    actual: WhatIfArm
    baseline: WhatIfArm
    variants: list[WhatIfArm] = []
    # How far the baseline replay sits from what the trades really booked —
    # the replay's own error, before any param is changed.
    replay_gap_r: float | None = None
    replay_same_rule_pct: float | None = None
    caveat: str = (
        "Entries are the live fills; exits are replayed at bar closes. A variant "
        "that holds longer is walked over bars the live trade never lived through, "
        "and fills pay no spread or slippage. Read diff_r against diff_se, never "
        "the expectancy column alone."
    )


def _stats(values: Sequence[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, sqrt(var / n)


def replay_exit(
    position: EnginePosition,
    bars: list[Bar],
    source: BarSource,
    params: dict[str, float],
    fill: FillMode,
) -> ReplayedExit | None:
    """Walk one live entry forward under `params`. None when the stored bars run
    out before any rule fires — a trade with no ending cannot be scored.

    The position is rebuilt from what the entry really was (price, size, ATR,
    instant); the stop and target are re-derived from the ATR through
    `stop_distance`, the line `plan_entry` uses, so baseline params reproduce
    the live stop exactly and a varied `stop_atr_mult` moves it the way a live
    config change would.
    """
    live_risk = position.risk_per_share
    if live_risk <= 0:
        return None
    risk = stop_distance(position.atr_at_entry, params)
    stop = position.entry_price - risk
    if risk <= 0 or stop <= 0:
        return None
    walker = EnginePosition(
        id=position.id,
        account_id=position.account_id,
        instrument_id=position.instrument_id,
        strategy=position.strategy,
        opened_at=position.opened_at,
        entry_price=position.entry_price,
        quantity=position.quantity,
        stop_price=stop,
        target_price=position.entry_price + risk * Decimal(str(params.get("target_r", 2.0))),
        atr_at_entry=position.atr_at_entry,
        trail_high=position.entry_price,
        last_bar_date=position.opened_at.date(),
    )
    # The first bar the trade lives through is the first one stamped after the
    # entry's own bucket — the same line `outcomes.window` draws, so a replayed
    # MFE and a stored one start on the same bar.
    minutes = source.minutes
    entry_bucket = (
        bucket_start(position.opened_at, minutes)
        if minutes is not None
        else datetime.combine(position.opened_at.date(), datetime.min.time())
    )
    first = next((j for j, b in enumerate(bars) if source.stamp(b) > entry_bucket), None)
    if first is None:
        return None
    for i in range(first, len(bars)):
        stamp = source.stamp(bars[i])
        walker.bars_held += 1
        walker.last_bar_date = stamp.date()
        # The backtest's clock, so the two walks read a bar at the same instant.
        now = stamp if source.is_intraday else datetime.combine(stamp.date(), _EVAL_TIME)
        fills, decision = step_exit(walker, bars, i, params, now, fill, source.timeframe)
        if decision is None:
            continue
        exit_price = fills[-1][0]
        walker.status = PositionStatus.CLOSED
        lived = bars[first : i + 1 if fill is FillMode.CLOSE else i]
        path = excursion(lived, walker.entry_price, live_risk, source, exit_price, stamp)
        return ReplayedExit(
            closed_at=stamp,
            exit_price=exit_price,
            rule=decision.rule,
            # Booked P&L per share of the original size, over the LIVE R.
            r=walker.booked_pnl / (walker.quantity * live_risk),
            mfe_r=path.mfe_r,
        )
    return None


def _live_mfe(position: EnginePosition, bars: list[Bar], source: BarSource) -> Decimal | None:
    """The best the live trade ever looked, walked the way `trd engine outcomes`
    walks it — so the actual arm's capture is the live capture."""
    lived, _ = window(bars, source, position.opened_at, position.closed_at)
    return excursion(
        lived,
        position.entry_price,
        position.risk_per_share,
        source,
        position.exit_price,
        position.closed_at,
    ).mfe_r


def _arm(
    label: str,
    rs: list[Decimal],
    mfes: list[Decimal | None],
    rules: list[str],
    params: dict[str, float] | None = None,
) -> WhatIfArm:
    floats = [float(r) for r in rs]
    mean, se = _stats(floats)
    return WhatIfArm(
        label=label,
        params=params or {},
        trades=len(rs),
        expectancy_r=mean,
        se_r=se,
        capture=pooled_capture(zip(mfes, rs, strict=True)),
        win_rate=(sum(1 for r in floats if r > 0) / len(floats) * 100) if floats else None,
        rules=dict(Counter(rules).most_common()),
    )


def compare(
    trades: list[tuple[EnginePosition, list[Bar]]],
    source: BarSource,
    base_params: dict[str, float],
    variants: list[dict[str, float]],
    fill: FillMode,
) -> tuple[WhatIfArm, WhatIfArm, list[WhatIfArm], dict[str, int], float | None, float | None]:
    """Replay every trade under the baseline and each variant; score them paired.

    A trade enters the comparison only if the baseline AND every variant can
    close it — a row scored on a different set of trades from its neighbours
    would not be paired at all.
    """
    excluded: Counter[str] = Counter()
    merged = [{**base_params, **v} for v in variants]
    kept: list[tuple[EnginePosition, ReplayedExit, list[ReplayedExit]]] = []
    bars_of = {position.id: bars for position, bars in trades}
    for position, bars in trades:
        base = replay_exit(position, bars, source, base_params, fill)
        if base is None:
            excluded["no ending in stored bars"] += 1
            continue
        runs = [replay_exit(position, bars, source, p, fill) for p in merged]
        if any(r is None for r in runs):
            excluded["a variant ran past the stored bars"] += 1
            continue
        kept.append((position, base, [r for r in runs if r is not None]))

    actual_r = [p.realized_r or Decimal(0) for p, _, _ in kept]
    actual = _arm(
        "actual",
        actual_r,
        [_live_mfe(p, bars_of[p.id], source) for p, _, _ in kept],
        # Trades closed before exit_rule was stored carry only prose; counted as
        # unrecorded rather than guessed at from the reason text.
        [p.exit_rule or "unrecorded" for p, _, _ in kept],
    )
    baseline = _arm(
        "baseline",
        [b.r for _, b, _ in kept],
        [b.mfe_r for _, b, _ in kept],
        [b.rule for _, b, _ in kept],
        {k: v for k, v in base_params.items() if k in DEFAULT_EXIT_PARAMS},
    )
    arms: list[WhatIfArm] = []
    for n, variant in enumerate(variants):
        runs = [v[n] for _, _, v in kept]
        arm = _arm(
            ", ".join(f"{k}={v:g}" for k, v in variant.items()),
            [r.r for r in runs],
            [r.mfe_r for r in runs],
            [r.rule for r in runs],
            variant,
        )
        diffs = [float(r.r - b.r) for r, (_, b, _) in zip(runs, kept, strict=True)]
        arm.diff_r, arm.diff_se = _stats(diffs)
        arm.changed = sum(1 for d in diffs if d != 0)
        arms.append(arm)

    gaps = [float(b.r - a) for (_, b, _), a in zip(kept, actual_r, strict=True)]
    gap, _ = _stats(gaps)
    recorded = [(p, b) for p, b, _ in kept if p.exit_rule]
    same = (
        sum(1 for p, b in recorded if p.exit_rule == b.rule) / len(recorded) * 100
        if recorded
        else None
    )
    return actual, baseline, arms, dict(excluded), gap, same


def parse_variants(raw: list[str]) -> list[dict[str, float]]:
    """`-p KEY=V1,V2` → one variant per value, each a single knob.

    One axis at a time, by construction: two -p flags produce two independent
    sweeps against the same baseline, never a grid. A grid of five knobs is
    thousands of rows over a few hundred trades, and the row that wins it is
    the row that fits the noise best.
    """
    variants: list[dict[str, float]] = []
    for item in raw:
        key, sep, values = item.partition("=")
        key = key.strip()
        if not sep or not key or not values.strip():
            raise TrdError(f"Expected KEY=VALUE[,VALUE...], got '{item}'.")
        if key in ENTRY_PARAMS:
            raise TrdError(
                f"{key} changes which trades exist, and a what-if holds the live entries "
                "fixed. Sweep it with 'trd engine backtest -p' instead."
            )
        if key not in DEFAULT_EXIT_PARAMS:
            known = sorted(set(DEFAULT_EXIT_PARAMS) - ENTRY_PARAMS)
            raise TrdError(f"Unknown exit param: {key}. Known: {', '.join(known)}.")
        for value in values.split(","):
            try:
                number = float(value)
            except ValueError as exc:
                raise TrdError(f"{key}: '{value}' is not a number.") from exc
            if number < 0:
                raise TrdError(f"{key} cannot be negative.")
            variants.append({key: number})
    return variants


class ExitWhatIfService:
    """Loads the live closed trades and their bars; the decisions are in
    `compare`. Takes a read-only connection — a what-if must never be able to
    hold the lock a scan needs."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.configs = EngineConfigRepo(conn)
        self.positions = EnginePositionRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)

    def config(self) -> EngineConfig:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")
        return config

    def run(
        self,
        variants: list[dict[str, float]],
        start: date | None = None,
        end: date | None = None,
        fill: FillMode | None = None,
    ) -> WhatIfResult:
        config = self.config()
        source = BarSource(self.prices, config.timeframe)
        fill = fill or default_fill(config.timeframe)
        day_engine = float(config.exit_params.get("flat_at_minute", 0)) > 0

        excluded: Counter[str] = Counter()
        series: dict[int, list[Bar]] = {}
        trades: list[tuple[EnginePosition, list[Bar]]] = []
        for position, _instrument in self.positions.list_closed(config.account_id):
            opened = position.opened_at.date()
            if (start and opened < start) or (end and opened > end):
                continue
            if position.closed_at is None or position.realized_r is None:
                excluded["no booked result"] += 1
                continue
            # A day trade that closed on a later session was carried by an outage,
            # not by a rule (SB-1054: four gap fills on 2026-09-10, -7.3R). The
            # replay would flatten it at the bell, so comparing the two would
            # credit every variant with fixing an outage.
            if day_engine and position.closed_at.date() != opened:
                excluded["carried past its session (outage)"] += 1
                continue
            if position.instrument_id not in series:
                series[position.instrument_id] = source.stored(position.instrument_id)
            trades.append((position, series[position.instrument_id]))

        if not trades:
            raise TrdError("No closed trades to replay in that window.")

        actual, baseline, arms, dropped, gap, same = compare(
            trades, source, dict(config.exit_params), variants, fill
        )
        excluded.update(dropped)
        return WhatIfResult(
            timeframe=config.timeframe,
            fill=fill,
            start=start,
            end=end,
            trades=baseline.trades,
            excluded=dict(excluded),
            actual=actual,
            baseline=baseline,
            variants=arms,
            replay_gap_r=gap,
            replay_same_rule_pct=same,
        )
