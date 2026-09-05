"""The judgement half of the decision review — the only part of trd that calls a model.

It reads the pack and the deterministic findings, and nothing else. Handed raw
price series a model produces confident narrative that cannot be reproduced or
tested; handed `MAE -1.4R at bar 3, capture 0.31, follow-through +2.2R` it has
something to argue from and a human can check the argument. That is the same
rule the rest of this project runs on: arguments from measurement survive,
arguments from recall decay.

Four things shape the design.

**Tools return pydantic models.** trd's whole read surface is already typed, so a
tool wrapping the pack hands back `TradeReview` and `Finding` objects rather than
prose the model has to parse. It also makes the model's reasoning traceable: the
tool calls are a record of what it actually looked at.

**Typed output, validated.** A weaker model that drifts fails validation loudly
instead of emitting plausible prose that reads fine and is wrong. That is what
makes "works with several models" a real claim rather than a hope.

**It is an adapter, never a service.** Nothing under `services/` imports this,
the same way nothing there imports Typer. The trading path must stay runnable —
and testable — with no API key, no network and no LLM SDK installed at all,
which is why the dependency is an optional extra.

**It proposes; it never touches anything.** Every tool is a read. There is no
tool that changes a stop, edits a config, adds a symbol or places an order — not
because the instructions forbid it, but because those tools do not exist here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, computed_field

# Imported at module scope, not inside the builder, because pydantic-ai resolves
# a tool's annotations against its module globals to build the schema — a local
# import leaves `RunContext` undefined at exactly the moment it is needed. The
# optionality of the dependency is therefore handled one level up: importing this
# module is what raises ImportError, and the CLI turns that into a sentence about
# the extra.
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model

from trd.errors import TrdError
from trd.services.review import (
    MIN_TRADES_FOR_FINDING,
    EnginePack,
    Finding,
    ReviewPack,
    ReviewResult,
    SignalReview,
    TradeReview,
)

# Opus 5 by default. Overridable because the point of building this on a
# model-agnostic framework is that the model is configuration — but a default
# that quietly drops to a cheaper model would make yesterday's review and
# today's incomparable without saying so, and the artefact records which ran.
DEFAULT_MODEL = "anthropic:claude-opus-5"

# Dollars per million tokens, read from the environment rather than baked in.
# A price table in source is exactly the kind of fact that goes stale silently —
# unset, the artefact reports tokens and says nothing about cost.
PRICE_IN_ENV = "TRD_AI_PRICE_IN"
PRICE_OUT_ENV = "TRD_AI_PRICE_OUT"

# How many rows a drill-down tool will hand back at once. A day engine fires
# hundreds of signals a session; the model needs a sample and the aggregates,
# not a transcript.
MAX_ROWS = 40

INSTRUCTIONS = """
You review a mechanical trading engine's decisions for its owner, once a day.

You are given a pack of measurements for one session and a set of findings that
were computed deterministically. Your job is to say what they mean together, and
to notice things a fixed detector cannot — a pattern across two strategies, a
finding that contradicts another, a result that is explained by the market rather
than by the rules.

Rules, in order of importance:

1. A finding is about a POPULATION — a rule, a strategy, the entry filter, a
   market condition — never about one trade. "SOFI stopped out, consider a wider
   stop" written every day buries the one real finding. If you can only support a
   claim with a single trade, do not make the claim.

2. Every claim rests on a named measurement from the pack: MAE, MFE, capture,
   follow-through, resolution, expectancy. Say which one and what its value was.
   If no measurement supports a thought, drop the thought.

3. Fewer than {min_trades} trades makes it a hypothesis, not a finding. Say so,
   and name the backtest that would settle it. `trd engine backtest --years 5`
   produces hundreds of trades per run where a session produces five.

4. "Nothing conclusive today" is a correct and expected answer. Two engines and a
   handful of trades a day are mostly noise. Set nothing_conclusive and return no
   findings rather than manufacturing one. You are not being paid by the finding.

5. Never recommend a change you cannot say how to test.

6. These are simulation fills with no slippage or spread, only closed trades have
   outcomes, and signals that fired with a full book could never have been taken.
   Do not draw a conclusion that depends on ignoring one of those.

Use the tools to look at the trades, the signals and the rules before you answer.
"""


class AiFinding(BaseModel):
    """One judgement, with what it rests on and how to test it."""

    headline: str = Field(description="One sentence. The claim itself, no hedging.")
    engine: str = Field(description="Which engine this is about.")
    scope: str = Field(description="rule | strategy | filter | regime | engine")
    subject: str = Field(description="The rule, strategy or thing being judged.")
    rationale: str = Field(description="Why, in two or three sentences.")
    rests_on: list[str] = Field(
        description="The measurements this rests on, named — e.g. 'capture 0.29 over 47 trades'."
    )
    trades: int = Field(description="How many trades or signals the claim is drawn from.")
    hypothesis: bool = Field(
        description=f"True when drawn from fewer than {MIN_TRADES_FOR_FINDING} trades."
    )
    test: str = Field(description="The command or experiment that would settle it.")


class AiReview(BaseModel):
    """What a model made of the day, on top of the arithmetic."""

    summary: str = Field(description="Two or three sentences. What today actually said.")
    nothing_conclusive: bool = Field(
        description="True when the honest answer is that nothing today was conclusive."
    )
    findings: list[AiFinding] = []
    watch_next: list[str] = Field(
        default=[],
        description="What would confirm or kill the above, if it played out over coming sessions.",
    )


class AiUsage(BaseModel):
    """What the run cost, in tokens and — only if prices are configured — dollars."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost_usd(self) -> Decimal | None:
        """None unless TRD_AI_PRICE_IN / _OUT are set.

        Deliberately not a table in source: model prices change, a stale one
        would be wrong in the confident direction, and a review that overstates
        its own cheapness is the last thing this should be.
        """
        try:
            price_in = Decimal(os.environ[PRICE_IN_ENV])
            price_out = Decimal(os.environ[PRICE_OUT_ENV])
        except (KeyError, ArithmeticError, ValueError):
            return None
        million = Decimal(1_000_000)
        return (
            Decimal(self.input_tokens) * price_in + Decimal(self.output_tokens) * price_out
        ) / million


class AiReviewRun(BaseModel):
    """The review, and everything needed to judge the review later."""

    review: AiReview
    usage: AiUsage


@dataclass
class ReviewDeps:
    """What the agent may look at. The whole world it has access to."""

    pack: ReviewPack
    computed: ReviewResult

    def engine(self, name: str | None) -> EnginePack | None:
        if name is None:
            return self.pack.engines[0] if self.pack.engines else None
        return next((e for e in self.pack.engines if e.engine == name), None)


def _brief(deps: ReviewDeps) -> str:
    """The prompt: small, and the same shape every day.

    Everything else is behind a tool. Pasting the whole pack in would cost a
    fortune on a day engine that fires 600 signals, and it would hide which parts
    the model actually read — the tool calls are that record.
    """
    lines = [f"Session under review: {deps.pack.on}. Engines: "]
    for pack in deps.pack.engines:
        day = pack.day
        lines.append(
            f"- {pack.engine} ({pack.config.timeframe}, {pack.config.strategies}): "
            f"{len(pack.trades)} trades closed today booking {day.realized_today:+.2f}, "
            f"{len(pack.signals)} signals fired, {len(pack.window_trades)} measured trades "
            f"and {len(pack.window_signals)} measured signals in the last "
            f"{pack.window_days} days. Open book: {day.open_positions} positions, "
            f"unrealized {day.unrealized:+.2f}, at risk {day.risk_at_stop:.2f}."
        )
    computed = deps.computed
    lines.append(
        f"\nThe deterministic pass produced {len(computed.findings)} finding(s), "
        f"{computed.hypotheses} of them hypotheses."
        if computed.findings
        else "\nThe deterministic pass found nothing conclusive."
    )
    lines.append("\nCaveats that apply to every number here:")
    lines.extend(f"- {c}" for c in computed.caveats)
    lines.append(
        "\nRead the deterministic findings and the trades before answering. "
        "Agreeing with the arithmetic is a fine answer; so is saying nothing today was conclusive."
    )
    return "\n".join(lines)


def build_agent(model: str | Model | None = None) -> Agent[ReviewDeps, AiReview]:
    """The review agent, with a read-only tool surface.

    Note what is NOT here: no tool that changes a stop, edits a config, adds a
    symbol or places an order. Read-only is a property of the tool surface, not a
    promise in the instructions.
    """
    from pydantic_ai.exceptions import UserError

    try:
        # Parametrized explicitly: the checker cannot infer the output type from
        # `output_type=AiReview` through the constructor's overloads.
        agent = Agent[ReviewDeps, AiReview](
            model or os.environ.get("TRD_AI_MODEL", "").strip() or DEFAULT_MODEL,
            deps_type=ReviewDeps,
            output_type=AiReview,
            instructions=INSTRUCTIONS.format(min_trades=MIN_TRADES_FOR_FINDING),
            retries=2,
        )
    except UserError as exc:
        # Almost always a missing key. Say where this deployment keeps it rather
        # than surfacing a stack trace from inside the framework.
        raise TrdError(
            f"{exc} trd reads it from the environment: export ANTHROPIC_API_KEY, "
            "on a laptop from the agents vault "
            "(op read 'op://agents/<item>/<field>'), and in the cluster from the "
            "same secret pattern the Telegram token uses."
        ) from None

    @agent.tool
    def deterministic_findings(ctx: RunContext[ReviewDeps]) -> list[Finding]:
        """The findings computed in code, with their evidence and sample sizes.

        Start here. These are already true; the question is what they mean
        together, and whether any of them contradicts another.
        """
        return ctx.deps.computed.findings

    @agent.tool
    def trades_closed(ctx: RunContext[ReviewDeps], engine: str | None = None) -> list[TradeReview]:
        """Every trade that closed in the session under review, best first.

        Each carries its entry reason as recorded at the time, the rule that
        closed it with that rule's stated intent, and its outcome metrics.
        """
        pack = ctx.deps.engine(engine)
        return pack.trades[:MAX_ROWS] if pack else []

    @agent.tool
    def signals_fired(
        ctx: RunContext[ReviewDeps], engine: str | None = None, acted: bool | None = None
    ) -> list[SignalReview]:
        """Signals from the session. `acted=False` gives the ones passed over.

        The passed signals are the only rows that can say whether the rules are
        filtering junk or discarding winners — but a signal that fired with the
        book already full was never a decision, and its outcome says so.
        """
        pack = ctx.deps.engine(engine)
        if pack is None:
            return []
        rows = [s for s in pack.signals if acted is None or s.acted == acted]
        return rows[:MAX_ROWS]

    @agent.tool
    def window_summary(ctx: RunContext[ReviewDeps], engine: str | None = None) -> dict[str, Any]:
        """Per-strategy aggregates over the trailing window — the population any
        claim has to be drawn from, since one session never is."""
        pack = ctx.deps.engine(engine)
        if pack is None:
            return {}
        out: dict[str, Any] = {}
        for row in pack.window_trades:
            bucket = out.setdefault(
                row.strategy,
                {"trades": 0, "offered_r": Decimal(0), "booked_r": Decimal(0), "worst_mae_r": None},
            )
            bucket["trades"] += 1
            bucket["offered_r"] += row.outcome.mfe_r or Decimal(0)
            bucket["booked_r"] += row.outcome.exit_r or Decimal(0)
            mae = row.outcome.mae_r
            if mae is not None and (bucket["worst_mae_r"] is None or mae < bucket["worst_mae_r"]):
                bucket["worst_mae_r"] = mae
        return {"window_days": pack.window_days, "by_strategy": out}

    @agent.tool
    def rule_book(ctx: RunContext[ReviewDeps], engine: str | None = None) -> dict[str, str]:
        """What each rule in play says it is for, in its own words.

        A decision is judged against what its rule claimed it would do, not
        against hindsight. A stop that hit is not a bad decision.
        """
        pack = ctx.deps.engine(engine)
        if pack is None:
            return {}
        book: dict[str, str] = {}
        for trade in pack.trades:
            book[trade.strategy] = trade.strategy_intent
            if trade.rule and trade.rule_intent:
                book[trade.rule] = trade.rule_intent
        for signal in pack.signals:
            book.setdefault(signal.strategy, signal.strategy_intent)
        return book

    return agent


def run_ai_review(
    pack: ReviewPack,
    computed: ReviewResult,
    model: str | Model | None = None,
    agent: Agent[ReviewDeps, AiReview] | None = None,
) -> AiReviewRun:
    """Ask a model what the day meant. Never called by anything in `services/`."""
    agent = agent or build_agent(model)
    deps = ReviewDeps(pack=pack, computed=computed)
    result = agent.run_sync(_brief(deps), deps=deps)
    usage = result.usage
    return AiReviewRun(
        review=result.output,
        usage=AiUsage(
            model=str(getattr(agent.model, "model_name", agent.model)),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            requests=getattr(usage, "requests", 0) or 0,
        ),
    )
