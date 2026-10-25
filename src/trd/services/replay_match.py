"""Backtest fidelity: does the replay trade the way the engine did? (SB-1342)

On the day engine's last thirty days the replay and the live engine each took 220
trades and came out 0.12R apart — larger than any effect the SB-1033 exit sweep
was looking for. A replay that cannot reproduce the trades already on the books
cannot be trusted with the ones that are not, so before any backtest is read for
a decision this says how far it sits from the truth, and why.

Same window, same rules, two books. Pair them — same symbol, same session,
nearest entry instant, each trade used once — and the gap in total R splits
exactly into four named parts:

    Σ replay R - Σ live R
      = Σ replay-only R                        (trades only the replay took)
      - Σ live-only R                          (trades only the engine took)
      + Σ matched (replay R - exit-replay R)   (ENTRY: same trade, different fill)
      + Σ matched (exit-replay R - live R)     (EXIT: same fill, different exit)

"exit-replay" is the live entry walked forward by the backtest's own exit step —
`exit_whatif.replay_exit` — so the entry and exit parts of a matched trade are
measured separately instead of argued about. The parts sum to the gap by
construction; a residual would mean the arithmetic is wrong, so it is computed
and must be zero.

Each trade is in its own R. A matched pair's two stops come from ATRs read a
few minutes apart and differ by a hair; the exit part, which compares the live
trade with its own replay, is in the live trade's R exactly.
"""

from collections import Counter
from datetime import date, datetime
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.engine.bars import BarSource
from trd.errors import TrdError
from trd.models import Bar, EnginePosition
from trd.repos import InstrumentRepo, PriceRepo
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo
from trd.services.backtest import (
    BacktestService,
    BacktestTrade,
    EntryFill,
    FillMode,
    default_entry_fill,
    default_fill,
)
from trd.services.exit_whatif import replay_exit


class GapPart(BaseModel):
    """One named piece of the gap: how many trades it covers and their total R."""

    label: str
    trades: int
    total_r: float
    mean_r: float | None = None


class ReplayMatchResult(BaseModel):
    start: date
    end: date
    timeframe: str
    fill: FillMode
    entry_fill: EntryFill = EntryFill.CLOSE
    live_trades: int
    replay_trades: int
    matched: int
    live_expectancy_r: float | None = None
    replay_expectancy_r: float | None = None
    # Σ replay R - Σ live R, and the four parts that make it up.
    gap_total_r: float
    parts: list[GapPart]
    residual_r: float
    # On matched pairs: how much later the live entry landed than the replay's
    # bar, in minutes, and whether the two picked the same strategy and exit.
    entry_offset_minutes_median: float | None = None
    same_strategy_pct: float | None = None
    same_exit_rule_pct: float | None = None
    # Unmatched trades by strategy — where the two books disagree about which
    # trades exist at all.
    live_only_by_strategy: dict[str, int] = {}
    replay_only_by_strategy: dict[str, int] = {}
    excluded: dict[str, int] = {}
    skipped: list[str] = []
    caveat: str = (
        "Each side in its own R. The replay fills entries at the signal bar's close; "
        "the live engine fills at a quote inside the forming bar, so a matched entry "
        "is the same setup read at two different instants."
    )


def _r(value: Decimal | None) -> float:
    return float(value) if value is not None else 0.0


def pair(
    live: list[tuple[EnginePosition, str]], replay: list[BacktestTrade]
) -> tuple[list[tuple[EnginePosition, BacktestTrade]], list[EnginePosition], list[BacktestTrade]]:
    """Match each live trade to the replay trade in the same symbol and session
    whose entry is nearest in time; each replay trade is used at most once.

    Greedy over the closest pairs first, so two trades in one name on one
    session pair with their own counterparts rather than whichever came first.
    """
    candidates: list[tuple[float, int, int]] = []
    for li, (position, symbol) in enumerate(live):
        for ri, trade in enumerate(replay):
            if trade.symbol == symbol and trade.entry_at.date() == position.opened_at.date():
                gap = abs((position.opened_at - trade.entry_at).total_seconds())
                candidates.append((gap, li, ri))
    candidates.sort()
    used_live: set[int] = set()
    used_replay: set[int] = set()
    matched: list[tuple[EnginePosition, BacktestTrade]] = []
    for _gap, li, ri in candidates:
        if li in used_live or ri in used_replay:
            continue
        used_live.add(li)
        used_replay.add(ri)
        matched.append((live[li][0], replay[ri]))
    live_only = [p for i, (p, _) in enumerate(live) if i not in used_live]
    replay_only = [t for i, t in enumerate(replay) if i not in used_replay]
    return matched, live_only, replay_only


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def decompose(
    live: list[tuple[EnginePosition, str]],
    replay: list[BacktestTrade],
    bars_for,
    source: BarSource,
    params: dict[str, float],
    fill: FillMode,
) -> tuple[list[GapPart], float, float, dict]:
    """Split Σ replay R - Σ live R into its four parts. Pure apart from
    `bars_for` (instrument id → stored series). Returns (parts, gap, residual,
    detail) where detail carries the matched-pair diagnostics."""
    matched, live_only, replay_only = pair(live, replay)

    entry_part: list[float] = []
    exit_part: list[float] = []
    offsets: list[float] = []
    same_strategy = same_rule = 0
    for position, trade in matched:
        replayed = replay_exit(position, bars_for(position.instrument_id), source, params, fill)
        # A live entry whose replay cannot close (bars ran out) is split as all
        # entry: the exit part is only defined when there is an exit to compare.
        exit_replay_r = float(replayed.r) if replayed is not None else _r(position.realized_r)
        entry_part.append(_r(trade.r_multiple) - exit_replay_r)
        exit_part.append(exit_replay_r - _r(position.realized_r))
        offsets.append((position.opened_at - trade.entry_at).total_seconds() / 60)
        same_strategy += position.strategy == trade.strategy
        same_rule += bool(position.exit_rule) and position.exit_rule == trade.rule

    replay_only_r = [_r(t.r_multiple) for t in replay_only]
    live_only_r = [_r(p.realized_r) for p in live_only]
    parts = [
        GapPart(
            label="replay-only trades",
            trades=len(replay_only),
            total_r=sum(replay_only_r),
            mean_r=_mean(replay_only_r),
        ),
        GapPart(
            label="live-only trades (subtracted)",
            trades=len(live_only),
            total_r=-sum(live_only_r),
            mean_r=_mean(live_only_r),
        ),
        GapPart(
            label="matched: entry (fill and timing)",
            trades=len(matched),
            total_r=sum(entry_part),
            mean_r=_mean(entry_part),
        ),
        GapPart(
            label="matched: exit (bar-close replay vs live)",
            trades=len(matched),
            total_r=sum(exit_part),
            mean_r=_mean(exit_part),
        ),
    ]
    gap = sum(_r(t.r_multiple) for t in replay) - sum(_r(p.realized_r) for p, _ in live)
    residual = gap - sum(part.total_r for part in parts)
    offsets.sort()
    detail = {
        "matched": len(matched),
        "offset": offsets[len(offsets) // 2] if offsets else None,
        "same_strategy": same_strategy / len(matched) * 100 if matched else None,
        "same_rule": same_rule / len(matched) * 100 if matched else None,
        "live_only": dict(Counter(p.strategy for p in live_only).most_common()),
        "replay_only": dict(Counter(t.strategy for t in replay_only).most_common()),
    }
    return parts, gap, residual, detail


class ReplayMatchService:
    """Runs the replay over a window and lays it against the live book. Read-only
    by connection, like the backtest it drives."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.configs = EngineConfigRepo(conn)
        self.positions = EnginePositionRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)

    def run(
        self, start: date, end: date | None = None, entry_fill: EntryFill | None = None
    ) -> ReplayMatchResult:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")
        source = BarSource(self.prices, config.timeframe)
        fill = default_fill(config.timeframe)
        entry_fill = entry_fill or default_entry_fill(config.timeframe)
        day_engine = float(config.exit_params.get("flat_at_minute", 0)) > 0

        excluded: Counter[str] = Counter()
        live: list[tuple[EnginePosition, str]] = []
        for position, instrument in self.positions.list_closed(config.account_id):
            opened = position.opened_at.date()
            if opened < start or (end is not None and opened > end):
                continue
            if position.realized_r is None:
                excluded["no booked result"] += 1
                continue
            # Same rule as the exit what-if: a day trade closed on a later session
            # was carried by an outage (SB-1054), which no replay reproduces.
            if day_engine and position.closed_at and position.closed_at.date() != opened:
                excluded["carried past its session (outage)"] += 1
                continue
            live.append((position, instrument.symbol))
        if not live:
            raise TrdError("No closed live trades in that window to compare against.")

        result = BacktestService(self.conn).run(
            start=start, end=end, fill=fill, entry_fill=entry_fill
        )
        replay = [
            t
            for t in result.trades
            if t.entry_at.date() >= start and (end is None or t.entry_at.date() <= end)
        ]

        series: dict[int, list[Bar]] = {}

        def bars_for(instrument_id: int) -> list[Bar]:
            if instrument_id not in series:
                series[instrument_id] = source.stored(instrument_id)
            return series[instrument_id]

        parts, gap, residual, detail = decompose(
            live, replay, bars_for, source, dict(config.exit_params), fill
        )
        live_r = [_r(p.realized_r) for p, _ in live]
        replay_r = [_r(t.r_multiple) for t in replay]
        return ReplayMatchResult(
            start=start,
            end=end or result.end,
            timeframe=config.timeframe,
            fill=fill,
            entry_fill=entry_fill,
            live_trades=len(live),
            replay_trades=len(replay),
            matched=detail["matched"],
            live_expectancy_r=_mean(live_r),
            replay_expectancy_r=_mean(replay_r),
            gap_total_r=gap,
            parts=parts,
            residual_r=residual,
            entry_offset_minutes_median=detail["offset"],
            same_strategy_pct=detail["same_strategy"],
            same_exit_rule_pct=detail["same_rule"],
            live_only_by_strategy=detail["live_only"],
            replay_only_by_strategy=detail["replay_only"],
            excluded=dict(excluded),
            skipped=result.skipped,
        )


def window_start(days: int, today: date | None = None) -> date:
    """The first entry date of a trailing window of `days` calendar days."""
    from datetime import timedelta

    return (today or datetime.now().date()) - timedelta(days=days)
