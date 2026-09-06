"""The decision review: one document per session, and the findings that need no model.

Two halves, deliberately separable.

**The pack** is everything a reviewer needs about one session, assembled
deterministically: every trade that closed with its SB-994 outcome, every signal
that fired — acted and not — with the reason recorded at the time, the rule that
fired quoted with its own stated intent, the config in force, and the day's
numbers from the same `engine_day` the Telegram report reads, so the two can
never disagree about whether it was an up day.

An agent is only reproducible if its input is. Handed a bundle it assembled
itself out of live queries, a model's answer cannot be re-run, diffed or
regression-tested, and "the review said something different today" becomes
unanswerable. So the pack is a document, and it is saved next to the review it
produced.

**The findings** are statistics, not judgement. Grouping outcomes by rule and
strategy, counting them, refusing to call three trades a pattern, and saying
"nothing conclusive" are all testable code. That is most of what a daily review
is for; a model's job is the part that genuinely needs one, and it consumes
exactly this.

Three rules run through everything here:

- **A finding names a population, never a trade.** "SOFI stopped out, consider a
  wider stop" written forty times a month buries the one real finding.
- **Small samples are hypotheses**, labelled as such, each carrying the backtest
  that would test it. A suggestion with no test attached is an opinion.
- **"Nothing conclusive" is the expected answer.** Two engines and a handful of
  trades a day; most daily variation is not signal. A reviewer that finds
  something every day is fitting noise.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from statistics import median

from pydantic import BaseModel, computed_field

from trd.build import build_version
from trd.engine import EXIT_REGISTRY
from trd.engine import REGISTRY as STRATEGIES
from trd.models import EngineConfig, SignalOutcome, TradeOutcome
from trd.services.daily_report import EngineDay, engine_day
from trd.services.engine import EngineService
from trd.services.outcomes import OutcomeService

# Below this many trades a pattern is not reported at all. Four trades agreeing
# is not a population, it is a coincidence with a narrative attached.
MIN_TRADES_TO_SPEAK = 5

# How far back a finding may be drawn from. A day is never a population; a month
# of sessions is enough to see a rule misbehave while it still matters.
DEFAULT_REVIEW_WINDOW = 30

# At or above this, a pattern is reported as a finding. Between the two it is a
# hypothesis: real enough to state, not enough to act on, and it carries the
# backtest that would settle it — which produces hundreds of trades per run.
MIN_TRADES_FOR_FINDING = 20

# Thresholds. Deliberately blunt and deliberately constants: a detector with a
# tuned threshold is a model with extra steps, and the point of this file is that
# every number in it can be argued with in one place.
LOW_CAPTURE = Decimal("0.30")  # keeping under a third of what was offered
EXITS_EARLY_R = Decimal("0.50")  # average follow-through this good means we left early
STOP_UNUSED_R = Decimal("0.60")  # worst heat any winner took, against a 1R stop
EARLY_PEAK_FRACTION = 0.4  # MFE this early in the hold means the exit is slow
# The entry filter's edge, in percentage points of 2R-first rate. Five points,
# not two: at 2 points the live swing engine produced a "finding" out of 2% of 40
# against 0% of 14 — one trade's difference, stated as though the ranking worked.
FILTER_EDGE_PCT = Decimal("5")
# And both sides need real weight before the comparison means anything at all.
MIN_SIGNALS_PER_SIDE = 30


class TradeReview(BaseModel):
    """One closed trade, whole: what happened, why, and what it did while on."""

    symbol: str
    strategy: str
    strategy_name: str
    # What the rule said it was for. Quoted from the registry, never re-derived:
    # a judgement about a decision has to be made against what the rule claimed
    # it would do, and the prose that claims it lives with the rule.
    strategy_intent: str
    entry_reason: str | None = None  # the signal's own words, recorded at entry
    opened_at: datetime
    closed_at: datetime | None = None
    bars_held: int = 0
    entry_price: Decimal
    exit_price: Decimal | None = None
    quantity: Decimal
    stop_price: Decimal
    target_price: Decimal
    pnl: Decimal | None = None
    r_multiple: Decimal | None = None
    rule: str | None = None
    rule_name: str | None = None
    rule_intent: str | None = None
    exit_reason: str | None = None
    outcome: TradeOutcome | None = None


class SignalReview(BaseModel):
    """One signal, taken or not, with what it went on to do."""

    symbol: str
    strategy: str
    strategy_name: str
    strategy_intent: str
    bar_ts: datetime
    fired_at: datetime
    price: Decimal
    score: float
    reason: str
    acted: bool
    outcome: SignalOutcome | None = None


class WindowTrade(BaseModel):
    """A measured trade inside the trailing window.

    `TradeOutcome` is keyed by position and knows nothing about which rules made
    the trade — deliberately, because the measurement is about a price path and
    should not need re-measuring when a rule is renamed. This is the join, made
    once when the pack is built rather than by every detector.
    """

    position_id: int
    strategy: str
    rule: str | None = None
    closed_on: date
    outcome: TradeOutcome


class WindowSignal(BaseModel):
    """A measured signal inside the trailing window, with when it fired."""

    strategy: str
    fired_on: date
    outcome: SignalOutcome


class ConfigSummary(BaseModel):
    """The rules in force when these decisions were made. A review of decisions
    taken under a 5-slot book reads differently from one taken under 10, and the
    config is not recoverable from the trades."""

    account: str
    timeframe: str
    position_size: Decimal
    sizing_mode: str
    max_positions: int
    max_entries_per_day: int
    earnings_blackout_days: int
    strategies: list[str]
    regime_sma: int = 0
    regime_vix_max: float = 0.0

    @classmethod
    def of(cls, config: EngineConfig, account: str) -> "ConfigSummary":
        return cls(
            account=account,
            timeframe=config.timeframe,
            position_size=config.position_size,
            sizing_mode=str(config.sizing_mode),
            max_positions=config.max_positions,
            max_entries_per_day=config.max_entries_per_day,
            earnings_blackout_days=config.earnings_blackout_days,
            strategies=list(config.strategies),
            regime_sma=int(config.exit_params.get("regime_sma", 0)),
            regime_vix_max=float(config.exit_params.get("regime_vix_max", 0)),
        )


class EnginePack(BaseModel):
    """One engine's session, complete enough to reason over without a second read."""

    engine: str
    on: date
    config: ConfigSummary
    day: EngineDay
    trades: list[TradeReview] = []
    signals: list[SignalReview] = []
    # Outcomes over the trailing window, which is what any statement about a
    # population has to be drawn from — one session is never enough.
    window_days: int
    window_trades: list[WindowTrade] = []
    window_signals: list[WindowSignal] = []
    unmeasured: int = 0  # closed trades with no outcome row yet

    @computed_field  # type: ignore[prop-decorator]
    @property
    def caveats(self) -> list[str]:
        out = [
            "Simulation fills: every entry and exit is assumed at a bar price. No "
            "slippage, no spread, no partial fills.",
        ]
        if self.unmeasured:
            out.append(
                f"{self.unmeasured} closed trade(s) have no outcome measured yet — run "
                "'trd engine outcomes --backfill'. Every population below excludes them."
            )
        if self.day.marks_are_stale:
            out.append("Marks are stale: the open book is priced at an older session.")
        return out


class ReviewPack(BaseModel):
    """Every engine's session, in one document, for one date."""

    on: date
    generated_at: datetime
    build: str
    engines: list[EnginePack] = []


class Finding(BaseModel):
    """One statement about a population, with the evidence and the way to test it.

    `n` and `scope` are not decoration. A reader has to be able to tell a claim
    drawn from 300 trades from one drawn from six without reading the prose, and
    the difference between those two is the difference between a finding and a
    guess.
    """

    key: str  # stable, so the same finding can be tracked across days
    engine: str
    scope: str  # strategy | rule | engine | filter
    subject: str
    headline: str
    detail: str
    n: int
    window_days: int
    evidence: dict[str, str] = {}
    # What would settle it. Every suggestion names its validation, or it is an
    # opinion — usually a backtest, which produces hundreds of trades per run
    # where a day produces five.
    test: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hypothesis(self) -> bool:
        """True when the sample is too small to act on. Stated, never hidden: a
        hypothesis that reads like a finding is how a backlog fills with work
        nobody should do."""
        return self.n < MIN_TRADES_FOR_FINDING


class ReviewResult(BaseModel):
    """The deterministic review of one session."""

    on: date
    generated_at: datetime
    build: str
    findings: list[Finding] = []
    engines: list[str] = []
    caveats: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quiet(self) -> bool:
        """Nothing conclusive. The expected answer on most days, and a reviewer
        whose quiet share drops to zero is fitting noise rather than finding
        signal."""
        return not self.findings

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hypotheses(self) -> int:
        return sum(1 for f in self.findings if f.hypothesis)


# ------------------------------------------------------------------- the pack


def _strategy_bits(key: str) -> tuple[str, str]:
    rule = STRATEGIES.get(key)
    if rule is None:
        return key, ""
    return rule.name, rule.description


def _rule_bits(key: str | None) -> tuple[str | None, str | None]:
    if key is None:
        return None, None
    rule = EXIT_REGISTRY.get(key)
    if rule is None:
        return key, None
    return rule.name, rule.description


def engine_pack(
    engine: EngineService,
    outcomes: OutcomeService,
    name: str,
    on: date,
    window_days: int = 30,
) -> EnginePack:
    """One engine's session, assembled from stored rows only.

    No network and no recomputation of anything a rule already said: the entry
    reason is the one recorded when the signal fired, and the rule's intent is
    the registry's own prose. Reading either back later must show what was
    actually claimed at the time, not what today's code would say about it.
    """
    config = engine.config()
    account = engine.account()
    day = engine_day(engine, name, on, window_days=window_days)

    measured = {o.position_id: o for o in outcomes.trades.list_all()}
    signal_measured = {o.signal_id: o for o in outcomes.signal_outcomes.list_all()}
    # Never past the date under review: a review of last Tuesday that quietly
    # used Thursday's trades would be unfalsifiable, and scoring old reviews
    # against what happened next is the whole reason they are stored.
    cutoff = on - timedelta(days=window_days)

    trades: list[TradeReview] = []
    window_trades: list[WindowTrade] = []
    unmeasured = 0
    for position, instrument in engine.positions.list_closed(account.id):
        outcome = measured.get(position.id)
        closed_on = position.closed_at.date() if position.closed_at else None
        if outcome is not None and closed_on is not None and cutoff < closed_on <= on:
            window_trades.append(
                WindowTrade(
                    position_id=position.id,
                    strategy=position.strategy,
                    rule=position.exit_rule,
                    closed_on=closed_on,
                    outcome=outcome,
                )
            )
        if closed_on != on:
            continue
        if outcome is None:
            unmeasured += 1
        signal = engine.signals.by_id(position.signal_id) if position.signal_id else None
        name_, intent = _strategy_bits(position.strategy)
        rule_name, rule_intent = _rule_bits(position.exit_rule)
        trades.append(
            TradeReview(
                symbol=instrument.symbol,
                strategy=position.strategy,
                strategy_name=name_,
                strategy_intent=intent,
                entry_reason=signal.reason if signal else None,
                opened_at=position.opened_at,
                closed_at=position.closed_at,
                bars_held=position.bars_held,
                entry_price=position.entry_price,
                exit_price=position.exit_price,
                quantity=position.quantity,
                stop_price=position.stop_price,
                target_price=position.target_price,
                pnl=position.realized_pnl,
                r_multiple=position.realized_r,
                rule=position.exit_rule,
                rule_name=rule_name,
                rule_intent=rule_intent,
                exit_reason=position.exit_reason,
                outcome=outcome,
            )
        )

    signals: list[SignalReview] = []
    window_signals: list[WindowSignal] = []
    for signal, instrument in engine.signals.list_recent(limit=1_000_000):
        fired_on = signal.fired_at.date()
        outcome = signal_measured.get(signal.id)
        if outcome is not None and cutoff < fired_on <= on:
            window_signals.append(
                WindowSignal(strategy=signal.strategy, fired_on=fired_on, outcome=outcome)
            )
        if fired_on != on:
            continue
        name_, intent = _strategy_bits(signal.strategy)
        signals.append(
            SignalReview(
                symbol=instrument.symbol,
                strategy=signal.strategy,
                strategy_name=name_,
                strategy_intent=intent,
                bar_ts=signal.bar_ts,
                fired_at=signal.fired_at,
                price=signal.price,
                score=signal.score,
                reason=signal.reason,
                acted=signal.acted,
                outcome=outcome,
            )
        )

    return EnginePack(
        engine=name,
        on=on,
        config=ConfigSummary.of(config, account.name),
        day=day,
        trades=sorted(trades, key=lambda t: t.r_multiple or Decimal(0), reverse=True),
        signals=signals,
        window_days=window_days,
        window_trades=window_trades,
        window_signals=window_signals,
        unmeasured=unmeasured,
    )


# --------------------------------------------------------------- the findings


def _by_strategy(pack: EnginePack) -> dict[str, list[TradeOutcome]]:
    """Window outcomes grouped by the strategy that opened the trade."""
    out: dict[str, list[TradeOutcome]] = {}
    for row in pack.window_trades:
        out.setdefault(row.strategy, []).append(row.outcome)
    return out


def pooled_capture(outcomes: list[TradeOutcome]) -> Decimal | None:
    """Booked R over offered R, pooled over the trades that were ever offered
    anything. A trade whose MFE is zero never had anything to keep, so it is in
    neither the numerator nor the denominator — which is why this can read +4%
    on a strategy whose booked total is negative. One definition, shared with
    the agent's window summary: two would hand a model a contradiction."""
    offered = [o for o in outcomes if o.mfe_r and o.mfe_r > 0 and o.exit_r is not None]
    total = sum((o.mfe_r or Decimal(0) for o in offered), Decimal(0))
    if not offered or total <= 0:
        return None
    return sum((o.exit_r or Decimal(0) for o in offered), Decimal(0)) / total


def _finding(
    engine: str,
    key: str,
    scope: str,
    subject: str,
    headline: str,
    detail: str,
    n: int,
    window_days: int,
    evidence: dict[str, str],
    test: str | None,
) -> Finding:
    return Finding(
        key=key,
        engine=engine,
        scope=scope,
        subject=subject,
        headline=headline,
        detail=detail,
        n=n,
        window_days=window_days,
        evidence=evidence,
        test=test,
    )


def capture_findings(pack: EnginePack) -> list[Finding]:
    """Strategies that find moves and are not paid for them.

    Capture is pooled, for the reason `trd learn capture` gives: averaging
    per-trade ratios divides by denominators that differ by orders of magnitude
    and invents findings that are not there.
    """
    out: list[Finding] = []
    for strategy, outcomes in sorted(_by_strategy(pack).items()):
        if len(outcomes) < MIN_TRADES_TO_SPEAK:
            continue
        capture = pooled_capture(outcomes)
        if capture is None or capture >= LOW_CAPTURE:
            continue
        offered = sum((o.mfe_r or Decimal(0) for o in outcomes), Decimal(0))
        booked = sum((o.exit_r or Decimal(0) for o in outcomes), Decimal(0))
        out.append(
            _finding(
                engine=pack.engine,
                key=f"capture.{strategy}",
                scope="strategy",
                subject=strategy,
                headline=(f"{strategy} keeps {float(capture) * 100:.0f}% of what it is offered"),
                detail=(
                    "The entries are finding moves and the exits are not being paid for "
                    "them. Low capture points at the exit rules, not the entry rule — "
                    "the same trades, taken the same way, with a different exit would "
                    "have a different result."
                ),
                n=len(outcomes),
                window_days=pack.window_days,
                evidence={
                    "offered_r": f"{offered:+.2f}",
                    "booked_r": f"{booked:+.2f}",
                    "capture": f"{float(capture) * 100:.0f}%",
                },
                test="trd engine backtest --years 5",
            )
        )
    return out


def early_exit_findings(pack: EnginePack) -> list[Finding]:
    """Exit rules that price keeps running past."""
    out: list[Finding] = []
    by_rule: dict[str, list[TradeOutcome]] = {}
    for row in pack.window_trades:
        # Only rows with a future to speak of: a trade whose horizon has not
        # filled in has no follow-through, and averaging its absence as zero
        # would drag every rule toward "the exit was perfectly timed".
        if row.rule is None or not row.outcome.follow_through_seen:
            continue
        by_rule.setdefault(row.rule, []).append(row.outcome)

    for rule, outcomes in sorted(by_rule.items()):
        if len(outcomes) < MIN_TRADES_TO_SPEAK:
            continue
        follows = [o.follow_through_r for o in outcomes if o.follow_through_r is not None]
        if not follows:
            continue
        mean = sum(follows, Decimal(0)) / Decimal(len(follows))
        if mean < EXITS_EARLY_R:
            continue
        name, _ = _rule_bits(rule)
        kept_going = sum(1 for o in outcomes if o.exited_early)
        out.append(
            _finding(
                engine=pack.engine,
                key=f"early-exit.{rule}",
                scope="rule",
                subject=rule,
                headline=f"{name or rule} exits leave {mean:+.2f}R on the table on average",
                detail=(
                    "Price kept going our way after this rule fired. That is the direct "
                    "reading of follow-through, and it is about the rule rather than any "
                    "one trade."
                ),
                n=len(outcomes),
                window_days=pack.window_days,
                evidence={
                    "avg_follow_through_r": f"{mean:+.2f}",
                    "kept_going_1r_or_more": f"{kept_going}/{len(outcomes)}",
                },
                test="trd engine backtest --years 5  (compare exit params)",
            )
        )
    return out


def slow_exit_findings(pack: EnginePack) -> list[Finding]:
    """Trades whose best moment came early and were held long past it."""
    out: list[Finding] = []
    for strategy, outcomes in sorted(_by_strategy(pack).items()):
        usable = [o for o in outcomes if o.mfe_bar and o.bars_seen > 2 and o.mfe_r and o.mfe_r > 0]
        if len(usable) < MIN_TRADES_TO_SPEAK:
            continue
        fractions = [(o.mfe_bar or 0) / o.bars_seen for o in usable]
        typical = median(fractions)
        if typical > EARLY_PEAK_FRACTION:
            continue
        out.append(
            _finding(
                engine=pack.engine,
                key=f"slow-exit.{strategy}",
                scope="strategy",
                subject=strategy,
                headline=(f"{strategy} peaks {typical * 100:.0f}% of the way into its holds"),
                detail=(
                    "The best moment of the median trade arrives early and the position "
                    "is carried well past it. That is an exit that is slow rather than "
                    "an entry that is wrong — the move the rule was looking for did "
                    "happen."
                ),
                n=len(usable),
                window_days=pack.window_days,
                evidence={"median_peak_fraction": f"{typical:.2f}"},
                test="trd engine backtest --years 5  (shorter max_sessions, or a trail)",
            )
        )
    return out


def stop_findings(pack: EnginePack) -> list[Finding]:
    """A stop paying for room no winner ever uses."""
    out: list[Finding] = []
    for strategy, outcomes in sorted(_by_strategy(pack).items()):
        winners = [o for o in outcomes if o.exit_r and o.exit_r > 0 and o.mae_r is not None]
        if len(winners) < MIN_TRADES_TO_SPEAK:
            continue
        worst = min((o.mae_r or Decimal(0)) for o in winners)
        if worst <= -STOP_UNUSED_R:
            continue
        out.append(
            _finding(
                engine=pack.engine,
                key=f"stop-room.{strategy}",
                scope="strategy",
                subject=strategy,
                headline=(f"no {strategy} winner took more than {abs(worst):.2f}R of heat"),
                detail=(
                    "The stop is one full R away and the winners never come close to it. "
                    "A tighter stop would risk less per trade for the same wins — which "
                    "under risk sizing means more size on the same idea. It would also "
                    "stop out some trades that recovered, and only a backtest can price "
                    "that trade-off."
                ),
                n=len(winners),
                window_days=pack.window_days,
                evidence={"worst_winner_mae_r": f"{worst:+.2f}"},
                test="trd engine backtest --years 5  (sweep stop_atr_mult)",
            )
        )
    return out


def filter_findings(pack: EnginePack) -> list[Finding]:
    """Whether the entry filter is earning its keep.

    Compares the signals the engine took against the ones it passed over *and
    could have taken*. Signals that fired with the book already full are excluded
    on both sides: they were never a decision, and counting them would make a
    capacity limit look like a judgement.
    """
    measured = [row.outcome for row in pack.window_signals]
    taken = [s for s in measured if s.acted and s.resolution]
    passed = [s for s in measured if not s.acted and not s.capacity_blocked and s.resolution]
    if len(taken) < MIN_SIGNALS_PER_SIDE or len(passed) < MIN_SIGNALS_PER_SIDE:
        return []

    def rate(rows: list[SignalOutcome]) -> Decimal:
        hits = sum(1 for s in rows if s.resolution == "target")
        return Decimal(hits) / Decimal(len(rows)) * 100

    taken_rate, passed_rate = rate(taken), rate(passed)
    gap = taken_rate - passed_rate
    if abs(gap) < FILTER_EDGE_PCT:
        return []

    better = gap > 0
    return [
        _finding(
            engine=pack.engine,
            key="filter.edge" if better else "filter.discards",
            scope="filter",
            subject="entry selection",
            headline=(
                f"signals we take reach 2R {gap:+.0f} points more often than those we skip"
                if better
                else f"signals we SKIP reach 2R {abs(gap):.0f} points more often than those taken"
            ),
            detail=(
                "Measured as which level came first, the 2R target or the 1R stop — not "
                "as an average return, which would score a signal on a path the engine "
                "would have been stopped out of. Signals that fired with the book full "
                "are excluded from both sides: they were never a decision."
                + (
                    ""
                    if better
                    else " This is the uncomfortable one: the ranking is discarding better "
                    "trades than it keeps."
                )
            ),
            # The weaker side, not the sum. A comparison is only as strong as its
            # smaller population, and adding them would let 2,000 passed signals
            # dress up a conclusion resting on fourteen taken ones.
            n=min(len(taken), len(passed)),
            window_days=pack.window_days,
            evidence={
                "taken_2r_first": f"{taken_rate:.0f}% of {len(taken)}",
                "passed_takeable_2r_first": f"{passed_rate:.0f}% of {len(passed)}",
            },
            test="trd engine backtest --years 5  (compare strategy sets)",
        )
    ]


DETECTORS = (
    capture_findings,
    early_exit_findings,
    slow_exit_findings,
    stop_findings,
    filter_findings,
)


def review(packs: list[EnginePack], on: date, generated_at: datetime | None = None) -> ReviewResult:
    """Every detector over every engine, with the caveats carried through.

    Findings are sorted by sample size, so the claims that rest on the most
    evidence are read first and a hypothesis never leads.
    """
    findings: list[Finding] = []
    caveats: list[str] = []
    for pack in packs:
        for detector in DETECTORS:
            findings.extend(detector(pack))
        # Named without brackets on purpose: this string is rendered through Rich,
        # where "[swing]" is markup and disappears, taking the attribution with it.
        caveats.extend(f"{pack.engine}: {c}" for c in pack.caveats)
    findings.sort(key=lambda f: (-f.n, f.engine, f.key))
    return ReviewResult(
        on=on,
        generated_at=generated_at or datetime.now(),
        build=build_version(),
        findings=findings,
        engines=[p.engine for p in packs],
        caveats=caveats,
    )
