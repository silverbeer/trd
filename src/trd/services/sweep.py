"""Parameter sweep: one command, one axis at a time, with the error bars on (SB-1345).

The SB-1033 sweep was 21 hand-run backtests on 21 copies of the database, with
expectancy's standard error and the split-half check worked out afterwards in
jq. The split-half check is what caught the only positive row of that table —
entry_delay_minutes=15 — as one half of the window carrying the whole result. A
check that decisive should not depend on someone remembering to run it.

Every row here is one knob changed against the stored config, replayed over the
same history:

- expectancy in R with its standard error, and its difference from the baseline
  with the SE of that difference (unpaired — the two rows take different trades,
  so nothing cancels);
- max drawdown, pooled capture and the exit-rule mix;
- expectancy over each half of the window, and a flag when the halves disagree
  about which side of the baseline the row sits on. A real effect shows up in
  both halves; an artefact of one stretch of tape does not.

Exit knobs that leave the entries alone are better measured paired with
`trd engine what-if`, whose SE is three times smaller. This is for the knobs
that change which trades exist — entry_delay_minutes, the regime gate — and for
seeing a whole-book number (drawdown) that a per-trade what-if cannot.
"""

import os
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from math import sqrt
from pathlib import Path
from typing import Any

from pydantic import BaseModel, computed_field

from trd.engine.exits import DEFAULT_EXIT_PARAMS
from trd.errors import TrdError
from trd.services.backtest import BacktestResult, BacktestService

# A row whose difference from the baseline is under this many standard errors is
# noise, whatever its sign — the same bar the what-if uses.
SIGNIFICANCE_SE = 2.0


class SweepRow(BaseModel):
    label: str
    overrides: dict[str, float] = {}
    trades: int
    expectancy_r: float | None = None
    se_r: float | None = None
    diff_r: float | None = None
    diff_se: float | None = None
    max_drawdown_pct: float
    capture: float | None = None
    return_pct: float | None = None
    rules: dict[str, int] = {}
    first_half_r: float | None = None
    second_half_r: float | None = None
    # The halves land on opposite sides of the baseline's own halves: the row's
    # whole difference comes from one stretch of the window.
    halves_disagree: bool | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def diff_t(self) -> float | None:
        if self.diff_r is None or not self.diff_se:
            return None
        return self.diff_r / self.diff_se

    @computed_field  # type: ignore[prop-decorator]
    @property
    def significant(self) -> bool | None:
        """Clears the bar: |t| >= SIGNIFICANCE_SE and the halves agree."""
        t = self.diff_t
        if t is None:
            return None
        return abs(t) >= SIGNIFICANCE_SE and self.halves_disagree is False


class SweepResult(BaseModel):
    start: date
    end: date
    split: date
    baseline: SweepRow
    rows: list[SweepRow]
    skipped: list[str] = []
    caveat: str = (
        "Unpaired: each row replays its own trades, so trade-to-trade noise does not "
        "cancel. |diff/se| >= 2 AND halves that agree is the bar; anything less is "
        "noise. Every number is a backtest's — an upper bound, not a forecast."
    )


def parse_axes(raw: list[str]) -> list[dict[str, float]]:
    """`-p KEY=V1,V2` → one single-knob variant per value; repeated -p are
    independent axes against the same baseline, never a grid."""
    variants: list[dict[str, float]] = []
    for item in raw:
        key, sep, values = item.partition("=")
        key = key.strip()
        if not sep or not key or not values.strip():
            raise TrdError(f"Expected KEY=VALUE[,VALUE...], got '{item}'.")
        if key not in DEFAULT_EXIT_PARAMS:
            raise TrdError(
                f"Unknown param: {key}. Known: {', '.join(sorted(DEFAULT_EXIT_PARAMS))}."
            )
        for value in values.split(","):
            try:
                number = float(value)
            except ValueError as exc:
                raise TrdError(f"{key}: '{value}' is not a number.") from exc
            if number < 0:
                raise TrdError(f"{key} cannot be negative.")
            variants.append({key: number})
    if not variants:
        raise TrdError("Nothing to sweep — pass at least one -p KEY=V1[,V2...].")
    return variants


def _mean_se(values: list[float]) -> tuple[float | None, float | None]:
    n = len(values)
    if n == 0:
        return None, None
    mean = sum(values) / n
    if n < 2:
        return mean, None
    return mean, sqrt(sum((v - mean) ** 2 for v in values) / (n - 1) / n)


def row_from(
    label: str, result: BacktestResult, overrides: dict[str, float], split: date
) -> SweepRow:
    rs = [float(t.r_multiple) for t in result.trades if t.r_multiple is not None]
    first = [
        float(t.r_multiple)
        for t in result.trades
        if t.r_multiple is not None and t.entry_at.date() < split
    ]
    second = [
        float(t.r_multiple)
        for t in result.trades
        if t.r_multiple is not None and t.entry_at.date() >= split
    ]
    mean, se = _mean_se(rs)
    return SweepRow(
        label=label,
        overrides=overrides,
        trades=len(result.trades),
        expectancy_r=mean,
        se_r=se,
        max_drawdown_pct=result.max_drawdown_pct,
        capture=float(result.capture) if result.capture is not None else None,
        return_pct=float(result.total_return_pct) if result.total_return_pct is not None else None,
        rules=dict(Counter(t.rule for t in result.trades).most_common()),
        first_half_r=_mean_se(first)[0],
        second_half_r=_mean_se(second)[0],
    )


def compare(baseline: SweepRow, row: SweepRow) -> SweepRow:
    """Fill in a row's difference from the baseline and the halves check."""
    if (
        row.expectancy_r is not None
        and baseline.expectancy_r is not None
        and row.se_r is not None
        and baseline.se_r is not None
    ):
        row.diff_r = row.expectancy_r - baseline.expectancy_r
        row.diff_se = sqrt(row.se_r**2 + baseline.se_r**2)
    a, b = row.first_half_r, row.second_half_r
    base_a, base_b = baseline.first_half_r, baseline.second_half_r
    if a is not None and b is not None and base_a is not None and base_b is not None:
        row.halves_disagree = (a - base_a > 0) != (b - base_b > 0)
    return row


def _run_one(db_path: str, kwargs: dict[str, Any]) -> BacktestResult:
    """One replay in a worker process, on its own read-only connection."""
    from trd.db.connection import connect_read_only

    return BacktestService(connect_read_only(Path(db_path))).run(**kwargs)


def sweep(
    db_path: Path,
    variants: list[dict[str, float]],
    start: date | None = None,
    end: date | None = None,
    workers: int | None = None,
) -> SweepResult:
    """Replay the baseline and every variant, in parallel processes when there
    is more than one worker. Each process opens its own read-only connection —
    DuckDB serves any number of readers — so the sweep never takes a lock."""
    jobs: list[dict[str, Any]] = [{"start": start, "end": end}]
    jobs += [{"start": start, "end": end, "params_override": v} for v in variants]
    workers = workers or max(1, min(len(jobs), (os.cpu_count() or 2) - 2, 6))
    if workers == 1:
        results = [_run_one(str(db_path), job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_run_one, [str(db_path)] * len(jobs), jobs))

    base_result = results[0]
    split = base_result.start + timedelta(days=(base_result.end - base_result.start).days // 2)
    baseline = row_from("baseline", base_result, {}, split)
    rows = [
        compare(
            baseline,
            row_from(", ".join(f"{k}={v:g}" for k, v in variant.items()), result, variant, split),
        )
        for variant, result in zip(variants, results[1:], strict=True)
    ]
    return SweepResult(
        start=base_result.start,
        end=base_result.end,
        split=split,
        baseline=baseline,
        rows=rows,
        skipped=base_result.skipped,
    )
