"""What a trade did while it was on, and what the signals we passed over did next.

The engine records the *result* of a trade and nothing about its shape. "Did we
enter too early?" and "did we exit too early?" are measurable questions, and
until this existed they could only be answered by opinion.

Four measurements, all in R, all a walk over stored bars:

- **MAE** — the worst it ever looked. A winner that first ran -0.9R was a bad
  entry that got lucky, and only this can tell it from a good one.
- **MFE** — the best it ever looked, and on which bar. The bar index carries
  half the meaning: a peak on bar 2 of a forty-bar hold indicts the exit rule,
  the same peak on the last bar exonerates it.
- **capture** — what fraction of the best was kept.
- **follow-through** — where price went after the exit, from the exit price.
  The direct answer to "did we get out too early".

And the one nobody asked for: the same walk over **signals the engine did not
take**. `engine_signal` has recorded every signal, acted or not, since the engine
existed, and nothing has ever read the unacted ones. They are the only rows in
this system that can say whether the rules filter junk or discard winners.

Two rules keep that honest. The counterfactual stop is the one `plan_entry`
would have set — the same function the live fill path uses, so a hypothetical R
and a real R are the same unit. And every passed signal records whether the book
was full when it fired: most of them could not have been taken, and counting
their returns as money left on the table would be fiction.

Nothing here calls an LLM, and nothing here judges. It is arithmetic that
something else can reason over.
"""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

import duckdb
from pydantic import BaseModel, computed_field

from trd.engine.bars import BarSource, bucket_start
from trd.errors import TrdError
from trd.models import (
    Bar,
    EngineConfig,
    EnginePosition,
    EngineSignal,
    Instrument,
    PositionStatus,
    SignalOutcome,
    TradeOutcome,
)
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo, EngineSignalRepo
from trd.repos.outcomes import SignalOutcomeRepo, TradeOutcomeRepo
from trd.timeframes import INTRADAY_MINUTES

# How far past the exit "did it keep working without us" looks.
#
# Five sessions on a swing engine — long enough for a trend to prove the exit
# wrong, short enough that it is still about this trade. One hour on an intraday
# one, because a day trade's whole life is measured in minutes and five sessions
# later is a different market. Both are "about as long again as the trade lived".
FOLLOW_THROUGH_SESSIONS = 5
FOLLOW_THROUGH_MINUTES = 60

# The stop and target a counterfactual is resolved against, in R. Deliberately
# the engine's own defaults: the question is what THIS engine would have done
# with the signal, not what an optimal trader would have.
COUNTERFACTUAL_STOP_R = Decimal(1)
COUNTERFACTUAL_TARGET_R = Decimal(2)


class Excursion(BaseModel):
    """How far a price path travelled either way, in R, and when."""

    mae_r: Decimal | None = None
    mae_at: datetime | None = None
    mae_bar: int | None = None
    mfe_r: Decimal | None = None
    mfe_at: datetime | None = None
    mfe_bar: int | None = None
    bars_seen: int = 0


class OutcomeGroup(BaseModel):
    """One population of trades or signals, averaged. Counts first: an average
    over three trades and one over three hundred are different claims."""

    label: str
    count: int = 0
    avg_mae_r: Decimal | None = None
    avg_mfe_r: Decimal | None = None
    avg_exit_r: Decimal | None = None
    # Pooled, never an average of ratios. Averaging per-trade capture divides by
    # denominators that differ by orders of magnitude: one trade that peaked at
    # +0.02R and stopped at -1R scores -50, and a handful of those drag the mean
    # to -507% — a number that looked like a finding on the live book and was an
    # artefact. Summing both sides first asks the question that actually matters:
    # of all the R this engine was ever offered, what share did it keep.
    capture_pooled: Decimal | None = None
    avg_follow_through_r: Decimal | None = None
    exited_early: int = 0  # follow-through of +1R or more after the exit
    target_first: int = 0
    stop_first: int = 0
    unresolved: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_first_pct(self) -> Decimal | None:
        resolved = self.target_first + self.stop_first + self.unresolved
        if resolved == 0:
            return None
        return Decimal(self.target_first) / Decimal(resolved) * 100


class OutcomeSummary(BaseModel):
    """Taken versus passed over, with the caveats that make the numbers readable.

    The caveats are output, not documentation. A number that quietly assumes a
    fill it never got, or averages only the trades that finished, is worse than
    no number, because it reads as a measurement.
    """

    timeframe: str
    closed_trades: int
    measured_trades: int
    open_trades: int
    signals_total: int
    measured_signals: int
    capacity_blocked: int
    follow_through_bars: int
    horizon_bars: int
    trades: OutcomeGroup
    taken: OutcomeGroup
    passed: OutcomeGroup
    passed_takeable: OutcomeGroup

    @computed_field  # type: ignore[prop-decorator]
    @property
    def caveats(self) -> list[str]:
        out = [
            "Simulation fills: entries and exits are assumed at bar prices. No "
            "slippage, no partial fills, no spread — MFE and capture flatter reality.",
        ]
        if self.open_trades:
            out.append(
                f"Survivorship: {self.open_trades} open trade(s) have no outcome yet, so "
                "every average here is over resolved trades only."
            )
        if self.capacity_blocked:
            out.append(
                f"{self.capacity_blocked} of the passed signals fired with the book already "
                "full — they could not have been taken, whatever they went on to do. "
                "'Passed (takeable)' excludes them; 'passed' does not."
            )
        return out


# --------------------------------------------------------------- the arithmetic


def follow_through_bars(timeframe: str) -> int:
    """How far past the exit to look, in bars of this engine's own timeframe."""
    minutes = INTRADAY_MINUTES.get(timeframe)
    if minutes is None:
        return FOLLOW_THROUGH_SESSIONS
    return max(1, FOLLOW_THROUGH_MINUTES // minutes)


def excursion(
    bars: Sequence[Bar],
    entry: Decimal,
    risk: Decimal,
    source: BarSource,
    exit_price: Decimal | None = None,
    exit_at: datetime | None = None,
) -> Excursion:
    """How far the path ran either way from `entry`, in units of `risk`.

    Lows for the adverse side and highs for the favourable one, because a trade
    lives through the whole of every bar it is in, not just its close. Measuring
    on closes would report a stop-out as a -0.4R day.

    The entry bar is deliberately NOT in `bars`: the engine fills at that bar's
    close, so its low already happened and counting it would credit the trade
    with an excursion it was never exposed to.

    The exit is a point on the path, not a bar, and it is folded in as one: the
    trade certainly visited `exit_price`, at `exit_at`. That matters on an
    intraday engine, where `window` leaves the bar the exit landed in OUT of
    `bars` — a 5-minute bar whose low printed after a 12:00:14 exit is not heat
    the trade took, and counting it once scored GOOGL at -7.5R on a trade that
    left at -1.8R. What happened inside that bar before the exit cannot be
    known from bar data; the exit price is the honest lower bound.
    """
    if risk <= 0 or (not bars and exit_price is None):
        return Excursion(bars_seen=len(bars))
    worst = best = None
    worst_i = best_i = 0
    for i, bar in enumerate(bars):
        if worst is None or bar.low < worst:
            worst, worst_i = bar.low, i
        if best is None or bar.high > best:
            best, best_i = bar.high, i
    worst_at = source.stamp(bars[worst_i]) if bars else None
    best_at = source.stamp(bars[best_i]) if bars else None
    if exit_price is not None:
        if worst is None or exit_price < worst:
            worst, worst_i, worst_at = exit_price, len(bars), exit_at
        if best is None or exit_price > best:
            best, best_i, best_at = exit_price, len(bars), exit_at
    assert worst is not None and best is not None
    return Excursion(
        # Clamped at zero on the sides that never happened: a trade that only
        # ever traded above its entry has no adverse excursion, and reporting a
        # positive MAE would invert the sign the column is read with.
        mae_r=min(Decimal(0), (worst - entry) / risk),
        mae_at=worst_at,
        mae_bar=worst_i + 1,
        mfe_r=max(Decimal(0), (best - entry) / risk),
        mfe_at=best_at,
        mfe_bar=best_i + 1,
        bars_seen=len(bars),
    )


def resolution(bars: Sequence[Bar], entry: Decimal, risk: Decimal) -> str | None:
    """Which came first inside the window: the 2R target or the 1R stop.

    The honest form of "what would that signal have done". An average forward
    return calls a signal good on a path that ran -1.2R first — a path the engine
    would have stopped out of and never seen the end of.

    A bar that spans both levels is scored as the stop. It is the pessimistic
    read, and it is the right one: bar data cannot say which came first inside
    the bar, and an outcome study that resolves its own ambiguity in its favour
    is worthless.
    """
    if risk <= 0 or not bars:
        return None
    stop = entry - risk * COUNTERFACTUAL_STOP_R
    target = entry + risk * COUNTERFACTUAL_TARGET_R
    for bar in bars:
        if bar.low <= stop:
            return "stop"
        if bar.high >= target:
            return "target"
    return "neither"


def window(
    bars: Sequence[Bar], source: BarSource, after: datetime, until: datetime | None
) -> tuple[list[Bar], list[Bar]]:
    """The bars a trade lived through, and the bars that came after it.

    Both ends are bar instants, not wall clocks: a position opened at 13:20 on a
    daily engine belongs to that session's bar, and one closed at 10:05 on a
    5-minute engine belongs to the 10:05 bucket. Comparing raw timestamps would
    put the entry bar inside the trade on one timeframe and outside it on the
    other.

    Which side of the line the EXIT bar falls on depends on the timeframe. A
    daily engine exits at the bell, after the whole bar has traded, so that bar
    was lived through. An intraday engine exits at a scan instant a few seconds
    into a five-minute bar, and the rest of that bar — most of it — is after the
    trade. So on intraday bars the exit bar is the first bar AFTER the trade:
    its low is follow-through, not heat, and its close is where "what happened
    next" starts. The exit price itself is folded in by `excursion`.
    """
    start = _bar_instant(after, source)
    end = _bar_instant(until, source) if until else None
    lived_through_exit_bar = source.minutes is None
    lived = [
        b
        for b in bars
        if source.stamp(b) > start
        and (
            end is None
            or source.stamp(b) < end
            or (lived_through_exit_bar and source.stamp(b) == end)
        )
    ]
    forward = [
        b
        for b in bars
        if end is not None
        and (source.stamp(b) > end or (not lived_through_exit_bar and source.stamp(b) == end))
    ]
    return lived, forward


def _bar_instant(moment: datetime, source: BarSource) -> datetime:
    minutes = source.minutes
    if minutes is None:
        return datetime.combine(moment.date(), datetime.min.time())
    return bucket_start(moment, minutes)


def trade_outcome(
    position: EnginePosition,
    bars: Sequence[Bar],
    source: BarSource,
    horizon: int,
    computed_at: datetime | None = None,
) -> TradeOutcome | None:
    """One closed trade's shape. None when it cannot honestly be measured."""
    risk = position.risk_per_share
    if risk <= 0 or position.closed_at is None:
        return None
    lived, forward = window(bars, source, position.opened_at, position.closed_at)
    ex = excursion(
        lived, position.entry_price, risk, source, position.exit_price, position.closed_at
    )

    exit_r = position.realized_r
    capture = None
    if exit_r is not None and ex.mfe_r is not None and ex.mfe_r > 0:
        capture = exit_r / ex.mfe_r

    seen = forward[:horizon]
    follow = None
    if seen and position.exit_price is not None:
        follow = (seen[-1].close - position.exit_price) / risk

    return TradeOutcome(
        position_id=position.id,
        computed_at=computed_at or datetime.now(),
        timeframe=source.timeframe,
        bars_seen=ex.bars_seen,
        risk_per_share=risk,
        mae_r=ex.mae_r,
        mae_at=ex.mae_at,
        mae_bar=ex.mae_bar,
        mfe_r=ex.mfe_r,
        mfe_at=ex.mfe_at,
        mfe_bar=ex.mfe_bar,
        exit_r=exit_r,
        capture=capture,
        follow_through_r=follow,
        follow_through_bars=horizon,
        follow_through_seen=len(seen),
    )


def signal_outcome(
    signal: EngineSignal,
    bars: Sequence[Bar],
    source: BarSource,
    risk: Decimal | None,
    horizon: int,
    open_positions: int,
    max_positions: int,
    computed_at: datetime | None = None,
) -> SignalOutcome | None:
    """What a signal did over the next `horizon` bars, taken or not.

    `risk` is the stop distance the engine would have used at that bar. Without
    it there is no R and therefore nothing comparable to say, so the signal is
    left unmeasured rather than measured in dollars.
    """
    if risk is None or risk <= 0:
        return None
    ahead = [b for b in bars if source.stamp(b) > signal.bar_ts][:horizon]
    ex = excursion(ahead, signal.price, risk, source)
    forward = (ahead[-1].close - signal.price) / risk if ahead else None
    return SignalOutcome(
        signal_id=signal.id,
        computed_at=computed_at or datetime.now(),
        timeframe=source.timeframe,
        acted=signal.acted,
        capacity_blocked=not signal.acted and open_positions >= max_positions,
        open_positions=open_positions,
        max_positions=max_positions,
        risk_per_share=risk,
        horizon_bars=horizon,
        bars_seen=len(ahead),
        mae_r=ex.mae_r,
        mfe_r=ex.mfe_r,
        mfe_bar=ex.mfe_bar,
        forward_r=forward,
        resolution=resolution(ahead, signal.price, risk),
    )


def _mean(values: list[Decimal | None]) -> Decimal | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present, Decimal(0)) / Decimal(len(present))


def _pooled_capture(outcomes: list[TradeOutcome]) -> Decimal | None:
    """Booked R divided by offered R, over every trade that was offered anything.

    Trades that never traded above their entry are excluded from BOTH sides: they
    had nothing to capture, and leaving them in would credit the engine with
    keeping a share of an opportunity that never existed.
    """
    offered = [o for o in outcomes if o.mfe_r is not None and o.mfe_r > 0 and o.exit_r is not None]
    if not offered:
        return None
    total_mfe = sum((o.mfe_r or Decimal(0) for o in offered), Decimal(0))
    if total_mfe <= 0:
        return None
    return sum((o.exit_r or Decimal(0) for o in offered), Decimal(0)) / total_mfe


def summarize_trades(label: str, outcomes: list[TradeOutcome]) -> OutcomeGroup:
    return OutcomeGroup(
        label=label,
        count=len(outcomes),
        avg_mae_r=_mean([o.mae_r for o in outcomes]),
        avg_mfe_r=_mean([o.mfe_r for o in outcomes]),
        avg_exit_r=_mean([o.exit_r for o in outcomes]),
        capture_pooled=_pooled_capture(outcomes),
        # Only trades that actually have a future: a trade closed yesterday has
        # no follow-through, and averaging its absence as zero would drag the
        # number toward "it went nowhere".
        avg_follow_through_r=_mean([o.follow_through_r for o in outcomes if o.follow_through_seen]),
        exited_early=sum(1 for o in outcomes if o.exited_early),
    )


def summarize_signals(label: str, outcomes: list[SignalOutcome]) -> OutcomeGroup:
    return OutcomeGroup(
        label=label,
        count=len(outcomes),
        avg_mae_r=_mean([o.mae_r for o in outcomes]),
        avg_mfe_r=_mean([o.mfe_r for o in outcomes]),
        avg_follow_through_r=_mean([o.forward_r for o in outcomes]),
        target_first=sum(1 for o in outcomes if o.resolution == "target"),
        stop_first=sum(1 for o in outcomes if o.resolution == "stop"),
        unresolved=sum(1 for o in outcomes if o.resolution == "neither"),
    )


class BackfillStats(BaseModel):
    """What a backfill pass did. Idempotent: run it twice and the second pass
    measures nothing, which is what `skipped` counts."""

    trades_measured: int = 0
    trades_skipped: int = 0
    signals_measured: int = 0
    signals_skipped: int = 0


# ------------------------------------------------------------------- the service


class OutcomeService:
    """Computes and stores outcomes. No provider and no network: every number
    here is a walk over bars already in the database, which is what makes it
    runnable after the close and repeatable to the digit."""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.configs = EngineConfigRepo(conn)
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)
        self.positions = EnginePositionRepo(conn)
        self.signals = EngineSignalRepo(conn)
        self.trades = TradeOutcomeRepo(conn)
        self.signal_outcomes = SignalOutcomeRepo(conn)

    def config(self) -> EngineConfig:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")
        return config

    def _account_id(self, config: EngineConfig) -> int:
        return config.account_id

    def backfill(self, recompute: bool = False, limit: int | None = None) -> BackfillStats:
        """Measure every closed trade and every signal that has none yet.

        Idempotent where it can be. A row is skipped only once it is FINAL —
        once the walk has seen its whole follow-through horizon. A trade that
        closed this afternoon has no future yet, so tonight's measurement stores
        `follow_through_seen = 0`; skipping it forever on that basis would make
        the engine permanently believe nothing ever happened after its exits.
        Those rows are re-measured each night until their horizon fills in, and
        then never again.

        `recompute` re-measures everything, for when the arithmetic changes.
        """
        config = self.config()
        source = BarSource(self.prices, config.timeframe)
        horizon = follow_through_bars(config.timeframe)
        stats = BackfillStats()
        now = datetime.now()

        # One bar series per instrument, not per trade: the walk is cheap and the
        # load is not, and a busy day engine has hundreds of trades in ten names.
        series: dict[int, list[Bar]] = {}

        done = set() if recompute else self.trades.final_ids()
        closed = [p for p, _ in self.positions.list_closed(self._account_id(config))]
        for position in closed:
            if position.id in done:
                stats.trades_skipped += 1
                continue
            outcome = trade_outcome(
                position,
                self._bars_for(series, source, position.instrument_id),
                source,
                horizon,
                computed_at=now,
            )
            if outcome is None:
                stats.trades_skipped += 1
                continue
            self.trades.upsert(outcome)
            stats.trades_measured += 1
            if limit and stats.trades_measured >= limit:
                break

        stats = self._backfill_signals(
            config, source, horizon, series, stats, now, recompute, limit
        )
        return stats

    def _backfill_signals(
        self,
        config: EngineConfig,
        source: BarSource,
        horizon: int,
        series: dict[int, list[Bar]],
        stats: BackfillStats,
        now: datetime,
        recompute: bool,
        limit: int | None,
    ) -> BackfillStats:
        done = set() if recompute else self.signal_outcomes.final_ids()
        # Every signal ever recorded. The unacted ones are the point, and there is
        # no cheaper way to ask what they did than to walk them.
        rows = self.signals.list_recent(limit=1_000_000)
        atr_cache: dict[int, dict[datetime, Decimal]] = {}
        for signal, instrument in rows:
            if signal.id in done:
                stats.signals_skipped += 1
                continue
            bars = self._bars_for(series, source, instrument.id)
            risk = self._risk_at(atr_cache, source, bars, instrument.id, signal.bar_ts, config)
            open_now = self.signal_outcomes.open_at(self._account_id(config), signal.fired_at)
            outcome = signal_outcome(
                signal,
                bars,
                source,
                risk,
                horizon,
                open_positions=open_now,
                max_positions=config.max_positions,
                computed_at=now,
            )
            if outcome is None:
                stats.signals_skipped += 1
                continue
            self.signal_outcomes.upsert(outcome)
            stats.signals_measured += 1
            if limit and stats.signals_measured >= limit:
                break
        return stats

    @staticmethod
    def _bars_for(series: dict[int, list[Bar]], source: BarSource, instrument_id: int) -> list[Bar]:
        if instrument_id not in series:
            series[instrument_id] = source.stored(instrument_id)
        return series[instrument_id]

    def _risk_at(
        self,
        cache: dict[int, dict[datetime, Decimal]],
        source: BarSource,
        bars: list[Bar],
        instrument_id: int,
        bar_ts: datetime,
        config: EngineConfig,
    ) -> Decimal | None:
        """The stop distance the engine would have set at that bar.

        The ATR series is computed once per instrument and indexed by bar instant.
        Calling `plan_entry` per signal would be the same arithmetic done from
        scratch on every prefix — quadratic over a day engine's tens of thousands
        of signals, for an identical answer.
        """
        if instrument_id not in cache:
            cache[instrument_id] = risk_series(bars, source, config.exit_params)
        return cache[instrument_id].get(bar_ts)

    # ----------------------------------------------------------------- reading

    def summary(self) -> OutcomeSummary:
        config = self.config()
        account_id = self._account_id(config)
        trades = self.trades.list_all()
        signals = self.signal_outcomes.list_all()
        taken = [s for s in signals if s.acted]
        passed = [s for s in signals if not s.acted]
        takeable = [s for s in passed if not s.capacity_blocked]
        return OutcomeSummary(
            timeframe=config.timeframe,
            closed_trades=len(self.positions.list_closed(account_id)),
            measured_trades=len(trades),
            open_trades=len(self.positions.list_open(account_id)),
            signals_total=len(signals),
            measured_signals=len(signals),
            capacity_blocked=sum(1 for s in passed if s.capacity_blocked),
            follow_through_bars=follow_through_bars(config.timeframe),
            horizon_bars=follow_through_bars(config.timeframe),
            trades=summarize_trades("closed trades", trades),
            taken=summarize_signals("signals taken", taken),
            passed=summarize_signals("signals passed", passed),
            passed_takeable=summarize_signals("passed (takeable)", takeable),
        )

    def trade_rows(
        self, limit: int | None = None
    ) -> list[tuple[TradeOutcome, EnginePosition, Instrument]]:
        return self.trades.joined(limit=limit)


def risk_series(
    bars: Sequence[Bar], source: BarSource, exit_params: dict[str, float]
) -> dict[datetime, Decimal]:
    """Stop distance per bar instant, the way `plan_entry` computes it.

    One ATR pass over the whole series, indexed by the instant each bar opened,
    so a counterfactual entry at any bar can be priced without re-deriving the
    indicator. The multiplier and the ATR period come from the same places the
    live fill path reads them.
    """
    # Imported here rather than at module scope: services.engine imports nothing
    # from this module, and keeping the arrow pointing one way is what stops a
    # cycle when either file grows.
    from trd.engine.base import indicator
    from trd.services.engine import stop_distance

    if not bars:
        return {}
    values = indicator("atr", bars, period=14)["value"]
    out: dict[datetime, Decimal] = {}
    for bar, atr in zip(bars, values, strict=False):
        if atr is None or atr <= 0:
            continue
        distance = stop_distance(Decimal(str(atr)), exit_params)
        if distance > 0:
            out[source.stamp(bar)] = distance
    return out


def open_at(positions: list[EnginePosition], moment: datetime) -> int:
    """How many of these were open at an instant. The in-memory twin of the repo
    query, for callers that already hold the positions."""
    return sum(
        1
        for p in positions
        if p.opened_at <= moment
        and (p.status == PositionStatus.OPEN or p.closed_at is None or p.closed_at > moment)
    )
