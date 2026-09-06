"""The judgement half of the decision review — the only part of trd that calls a model.

It reads the pack and the deterministic findings, and nothing else. Handed raw
price series a model produces confident narrative that cannot be reproduced or
tested; handed `MAE -1.4R at bar 3, capture 0.31, follow-through +2.2R` it has
something to argue from and a human can check the argument. That is the same
rule the rest of this project runs on: arguments from measurement survive,
arguments from recall decay.

Five things shape the design.

**Tools return pydantic models — projections, not storage rows.** trd's whole
read surface is already typed, so a tool hands back a typed row rather than
prose the model has to parse. But it hands back `TradeBrief`, not `TradeReview`:
the fields a judgement uses, rounded to what a judgement can use. The first
version returned the storage model whole — forty trades with nested outcomes at
full Decimal precision — and cost $1.38 a run, most of it a model reading thirty
fields to find four. The tool calls are still a record of what it looked at.

**Typed output, validated.** A weaker model that drifts fails validation loudly
instead of emitting plausible prose that reads fine and is wrong. That is what
makes "works with several models" a real claim rather than a hope.

**The stable prefix is cached and the round trips are bounded.** Instructions,
tool definitions and the brief's shape are identical every night, which is the
shape prompt caching exists for; every request after the first re-reads that
prefix at a tenth of the price instead of paying for it again. `AiUsage`
records the cache reads so that claim can be checked on every run — a cache
read of zero means something in the prefix is varying and the caching is
theatre. And the model gets a fixed number of turns: an unbounded agent took
eight to ten, and the ninth bought nothing the third had not.

**It is an adapter, never a service.** Nothing under `services/` imports this,
the same way nothing there imports Typer. The trading path must stay runnable —
and testable — with no API key, no network and no LLM SDK installed at all,
which is why the dependency is an optional extra. Which backend runs is decided
in one function (`resolve_model`): pydantic-ai's own `provider:model` strings
pass straight through, and trd adds two schemes of its own — `claude-code:` for
the subscription (the dev loop, which should cost nothing per token) and
`vertex:` for an unattended run billed to a cloud project. Nothing else in trd
learns which one it got.

**It proposes; it never touches anything.** Every tool is a read. There is no
tool that changes a stop, edits a config, adds a symbol or places an order — not
because the instructions forbid it, but because those tools do not exist here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from pydantic import BaseModel, Field, computed_field
from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import FunctionToolset, ToolsetTool, WrapperToolset
from pydantic_ai.usage import UsageLimits

from trd.errors import TrdError
from trd.models import TradeOutcome
from trd.services.review import (
    MIN_TRADES_FOR_FINDING,
    EnginePack,
    Finding,
    ReviewPack,
    ReviewResult,
    SignalReview,
    TradeReview,
    pooled_capture,
)

DEFAULT_MODEL = "anthropic:claude-opus-5"

PRICE_IN_ENV = "TRD_AI_PRICE_IN"
PRICE_OUT_ENV = "TRD_AI_PRICE_OUT"
# Per-MTok prices for the two cache lanes. Optional: when unset they derive from
# the input price by Anthropic's published ratios below. Set them when the
# provider's ratio differs — a Vertex bill, a model with a different cache rate.
PRICE_CACHE_READ_ENV = "TRD_AI_PRICE_CACHE_READ"
PRICE_CACHE_WRITE_ENV = "TRD_AI_PRICE_CACHE_WRITE"
CACHE_READ_RATIO = Decimal("0.1")  # a cache read costs a tenth of an uncached token
CACHE_WRITE_RATIO = Decimal("1.25")  # a 5-minute cache write costs a quarter more

VERTEX_PROJECT_ENV = "TRD_AI_VERTEX_PROJECT"
VERTEX_REGION_ENV = "TRD_AI_VERTEX_REGION"
VERTEX_DEFAULT_REGION = "global"

# The cache TTL. Five minutes covers every round trip inside one run, which is
# where the saving is: a nightly run is never within an hour of the last one.
CACHE_TTL = "5m"

# How many turns of tool use a review gets. Measured: an unbounded run took 8-10
# requests and the later ones added nothing. Past this a tool call is refused
# (see BoundedToolset) so the model answers on the next turn rather than being
# cut off; the hard limit sits two above to leave room for a validation retry.
MAX_REQUESTS = 5
USAGE_LIMITS = UsageLimits(request_limit=MAX_REQUESTS + 2)

MAX_ROWS = 40
_PLACES = Decimal("0.01")

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

You have {max_requests} turns of tool use; after that a call is refused and you
must answer. The brief already carries the deterministic findings and the
per-strategy window summary — do not fetch them again. Ask for everything else
you want — trades, signals, the rule book, for each engine — as parallel tool
calls in your first turn, then answer.
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
    """What the run cost, in tokens and — only if prices are configured — dollars.

    `input_tokens` is the total the provider counted, and the two cache figures
    are the part of it that went through the cache. They are recorded, not
    summarised away, because the whole cost argument rests on them: a run whose
    `cache_read_tokens` is zero after the first request is paying full price for
    its own history, whatever the settings claim.
    """

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def uncached_input_tokens(self) -> int:
        """Input tokens billed at the full rate."""
        return max(self.input_tokens - self.cache_read_tokens - self.cache_write_tokens, 0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost_usd(self) -> Decimal | None:
        """None unless TRD_AI_PRICE_IN / _OUT are set.

        Deliberately not a table in source: model prices change, a stale one
        would be wrong in the confident direction, and a review that overstates
        its own cheapness is the last thing this should be. The cache lanes
        default to Anthropic's published ratios of the input price and can be
        overridden per MTok when a provider bills them differently.
        """
        try:
            price_in = Decimal(os.environ[PRICE_IN_ENV])
            price_out = Decimal(os.environ[PRICE_OUT_ENV])
            price_read = Decimal(
                os.environ.get(PRICE_CACHE_READ_ENV) or price_in * CACHE_READ_RATIO
            )
            price_write = Decimal(
                os.environ.get(PRICE_CACHE_WRITE_ENV) or price_in * CACHE_WRITE_RATIO
            )
        except (KeyError, ArithmeticError, ValueError):
            return None
        million = Decimal(1_000_000)
        return (
            Decimal(self.uncached_input_tokens) * price_in
            + Decimal(self.cache_read_tokens) * price_read
            + Decimal(self.cache_write_tokens) * price_write
            + Decimal(self.output_tokens) * price_out
        ) / million


class AiReviewRun(BaseModel):
    """The review, and everything needed to judge the review later."""

    review: AiReview
    usage: AiUsage


# ------------------------------------------------------------ the tool payloads


def _q(value: Decimal | None) -> Decimal | None:
    """Two places. A judgement cannot use the eighth decimal of an R-multiple,
    and a model reading it pays for every digit."""
    return None if value is None else value.quantize(_PLACES)


class TradeBrief(BaseModel):
    """One closed trade, reduced to what a judgement about it uses.

    Symbol and strategy to group by; the rule that closed it; R, MAE, MFE,
    capture and follow-through to judge it by; the entry reason because it is
    the rule's own words at the time. Not: prices, quantities, timestamps, the
    rule's intent (that is `rule_book`), or anything to eight places.
    """

    symbol: str
    strategy: str
    rule: str | None = None
    bars_held: int = 0
    r: Decimal | None = None
    mae_r: Decimal | None = None
    mfe_r: Decimal | None = None
    capture: Decimal | None = None
    after_r: Decimal | None = None  # follow-through: where price went after the exit
    entry_reason: str | None = None

    @classmethod
    def of(cls, trade: TradeReview) -> TradeBrief:
        o = trade.outcome
        return cls(
            symbol=trade.symbol,
            strategy=trade.strategy,
            rule=trade.rule,
            bars_held=trade.bars_held,
            r=_q(trade.r_multiple),
            mae_r=_q(o.mae_r) if o else None,
            mfe_r=_q(o.mfe_r) if o else None,
            capture=_q(o.capture) if o else None,
            after_r=_q(o.follow_through_r) if o and o.follow_through_seen else None,
            entry_reason=trade.entry_reason,
        )


class SignalBrief(BaseModel):
    """One signal, reduced to whether it was taken, whether it could have been,
    and what it did next."""

    symbol: str
    strategy: str
    acted: bool
    blocked: bool = False  # fired with the book full — never a decision
    score: float
    resolution: str | None = None  # target | stop | neither, inside the horizon
    forward_r: Decimal | None = None
    mfe_r: Decimal | None = None
    mae_r: Decimal | None = None

    @classmethod
    def of(cls, signal: SignalReview) -> SignalBrief:
        o = signal.outcome
        return cls(
            symbol=signal.symbol,
            strategy=signal.strategy,
            acted=signal.acted,
            blocked=bool(o and o.capacity_blocked),
            score=round(signal.score, 2),
            resolution=o.resolution if o else None,
            forward_r=_q(o.forward_r) if o else None,
            mfe_r=_q(o.mfe_r) if o else None,
            mae_r=_q(o.mae_r) if o else None,
        )


class StrategyWindow(BaseModel):
    """One strategy's closed trades over the trailing window, pooled."""

    trades: int = 0
    offered_r: Decimal = Decimal(0)
    booked_r: Decimal = Decimal(0)
    # Booked over offered among the trades that were offered anything — the
    # detector's own definition (`pooled_capture`), so the number here and the
    # number in a finding are the same number. It is not booked_r / offered_r:
    # a pure loser has no MFE and sits in neither side of the ratio.
    capture: Decimal | None = None
    worst_mae_r: Decimal | None = None
    exited_early: int = 0  # trades where price ran a further 1R after the exit


class SignalWindow(BaseModel):
    """One strategy's signals over the window: taken, takeable-but-passed, and
    blocked by a full book, with how often each side reached 2R before 1R."""

    fired: int = 0
    taken: int = 0
    passed_takeable: int = 0
    passed_blocked: int = 0
    taken_target_first: int = 0
    passed_takeable_target_first: int = 0


class WindowSummary(BaseModel):
    """The population any claim has to be drawn from, since one session never is."""

    window_days: int
    by_strategy: dict[str, StrategyWindow] = {}
    signals: dict[str, SignalWindow] = {}


def window_summary_of(pack: EnginePack) -> WindowSummary:
    out = WindowSummary(window_days=pack.window_days)
    outcomes: dict[str, list[TradeOutcome]] = {}
    for row in pack.window_trades:
        outcomes.setdefault(row.strategy, []).append(row.outcome)
    for strategy, rows in outcomes.items():
        maes = [o.mae_r for o in rows if o.mae_r is not None]
        out.by_strategy[strategy] = StrategyWindow(
            trades=len(rows),
            offered_r=_q(sum((o.mfe_r or Decimal(0) for o in rows), Decimal(0))) or Decimal(0),
            booked_r=_q(sum((o.exit_r or Decimal(0) for o in rows), Decimal(0))) or Decimal(0),
            capture=_q(pooled_capture(rows)),
            worst_mae_r=_q(min(maes)) if maes else None,
            exited_early=sum(1 for o in rows if o.exited_early),
        )
    for row in pack.window_signals:
        o = row.outcome
        sig = out.signals.setdefault(row.strategy, SignalWindow())
        sig.fired += 1
        hit = o.resolution == "target"
        if o.acted:
            sig.taken += 1
            sig.taken_target_first += hit
        elif o.capacity_blocked:
            sig.passed_blocked += 1
        else:
            sig.passed_takeable += 1
            sig.passed_takeable_target_first += hit
    return out


# ---------------------------------------------------------------------- the deps


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

    It carries what every review needs — the deterministic findings and the
    per-strategy window — so the model does not spend two round trips fetching
    what it was always going to fetch. The rows stay behind tools: pasting the
    whole pack in would cost a fortune on a day engine that fires 600 signals,
    and it would hide which parts the model actually read.
    """
    lines = [f"Session under review: {deps.pack.on}. Engines:"]
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
        summary = window_summary_of(pack)
        for strategy, w in summary.by_strategy.items():
            capture = f"{w.capture:.0%}" if w.capture is not None else "n/a"
            lines.append(
                f"  {pack.engine}/{strategy} over {summary.window_days}d: {w.trades} trades, "
                f"offered {w.offered_r:+.1f}R, booked {w.booked_r:+.1f}R, capture {capture} "
                f"(booked over offered, among trades ever offered anything), "
                f"worst MAE {w.worst_mae_r}R, {w.exited_early} exited early."
            )
        for strategy, s in summary.signals.items():
            lines.append(
                f"  {pack.engine}/{strategy} signals: {s.fired} fired, {s.taken} taken "
                f"({s.taken_target_first} hit 2R first), {s.passed_takeable} passed while "
                f"takeable ({s.passed_takeable_target_first} hit 2R first), "
                f"{s.passed_blocked} passed with the book full."
            )
    computed = deps.computed
    if computed.findings:
        lines.append(
            f"\nThe deterministic pass produced {len(computed.findings)} finding(s), "
            f"{computed.hypotheses} of them hypotheses:"
        )
        for f in computed.findings:
            label = "HYPOTHESIS" if f.hypothesis else "FINDING"
            evidence = ", ".join(f"{k} {v}" for k, v in f.evidence.items())
            lines.append(
                f"- {label} {f.engine}/{f.scope} {f.subject} (n={f.n}): {f.headline}"
                + (f" [{evidence}]" if evidence else "")
            )
    else:
        lines.append("\nThe deterministic pass found nothing conclusive.")
    lines.append("\nCaveats that apply to every number here:")
    lines.extend(f"- {c}" for c in computed.caveats)
    lines.append(
        "\nRead the trades and the signals before answering. Agreeing with the "
        "arithmetic is a fine answer; so is saying nothing today was conclusive."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------- the seam


def resolve_model(model: str | Model | None) -> str | Model:
    """Turn a model setting into something pydantic-ai can run.

    pydantic-ai's own `provider:model` strings pass straight through — that is
    thirty-odd providers for free. trd adds two schemes on top, neither of which
    the runtime image has to carry:

    - `claude-code:<opus|sonnet|...>` runs the local `claude` binary on the
      subscription. No key, no per-token bill, no extra to install; the dev loop.
    - `vertex:<model>` bills a Google Cloud project, authenticated by gcloud
      ADC. Project from TRD_AI_VERTEX_PROJECT (else GOOGLE_CLOUD_PROJECT),
      region from TRD_AI_VERTEX_REGION (else 'global').

    Nothing else in trd learns which one it got.
    """
    if isinstance(model, Model):
        return model
    name = model or os.environ.get("TRD_AI_MODEL", "").strip() or DEFAULT_MODEL
    scheme, _, rest = name.partition(":")
    if scheme == "claude-code":
        # No extra: it needs nothing installed beyond the binary, which the
        # adapter checks for at the first request rather than here.
        from trd.agents.claude_code import ClaudeCodeModel

        return ClaudeCodeModel(rest or "opus")
    if scheme == "vertex":
        try:
            from anthropic import AsyncAnthropicVertex
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider
        except ImportError:
            raise TrdError(
                "'vertex:' needs the optional extra: uv sync --extra vertex "
                "(or pip install 'trd[vertex]'), and gcloud application-default credentials."
            ) from None
        project = os.environ.get(VERTEX_PROJECT_ENV) or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project or not rest:
            raise TrdError(
                f"'vertex:<model>' needs a model name and a project: set {VERTEX_PROJECT_ENV} "
                "(or GOOGLE_CLOUD_PROJECT)."
            )
        region = os.environ.get(VERTEX_REGION_ENV) or VERTEX_DEFAULT_REGION
        client = AsyncAnthropicVertex(project_id=project, region=region)
        return AnthropicModel(rest, provider=AnthropicProvider(anthropic_client=client))
    return name


# Cache the tools, the instructions and — as the conversation grows — the last
# block, so every request after the first re-reads its own history at a tenth
# of the price. Keys are provider-prefixed by pydantic-ai convention: a model
# that is not Anthropic's ignores them, and the settings cost nothing there.
MODEL_SETTINGS = cast(
    ModelSettings,
    {
        "anthropic_cache_instructions": CACHE_TTL,
        "anthropic_cache_tool_definitions": CACHE_TTL,
        "anthropic_cache": CACHE_TTL,
    },
)


class BoundedToolset(WrapperToolset[ReviewDeps]):
    """The bound, made graceful — and made cheap.

    Past the last permitted turn a tool call is not executed; it returns a
    sentence saying the turns are spent, and the model's only move left is to
    answer. A hard request limit alone would cut the run off mid-thought and
    cost the night's review.

    The tools are refused, never withdrawn: tool definitions sit at the front of
    the cached prefix, and the first version of this — which removed them on the
    last turn — invalidated the whole cache on the most expensive request of the
    run, at $0.12 a night. Same bound, one refused call, the prefix intact.
    """

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[ReviewDeps],
        tool: ToolsetTool[ReviewDeps],
    ) -> Any:
        if ctx.usage.requests >= MAX_REQUESTS:
            return (
                f"No turns left: this was turn {ctx.usage.requests} of {MAX_REQUESTS}. "
                "Answer now, through the output tool, with what you already have."
            )
        return await super().call_tool(name, tool_args, ctx, tool)


def build_toolset() -> FunctionToolset[ReviewDeps]:
    """The read-only tool surface, on its own so a test can list it.

    Note what is NOT here: no tool that changes a stop, edits a config, adds a
    symbol or places an order. Read-only is a property of the tool surface, not a
    promise in the instructions.
    """
    toolset = FunctionToolset[ReviewDeps]()

    @toolset.tool
    def deterministic_findings(ctx: RunContext[ReviewDeps]) -> list[Finding]:
        """The findings computed in code, with their evidence and sample sizes.

        The brief already lists them; this is the full form, with the detail
        and the test each one names.
        """
        return ctx.deps.computed.findings

    @toolset.tool
    def trades_closed(ctx: RunContext[ReviewDeps], engine: str | None = None) -> list[TradeBrief]:
        """Every trade that closed in the session under review, best first:
        the rule that closed it, R, MAE, MFE, capture, follow-through."""
        pack = ctx.deps.engine(engine)
        return [TradeBrief.of(t) for t in pack.trades[:MAX_ROWS]] if pack else []

    @toolset.tool
    def signals_fired(
        ctx: RunContext[ReviewDeps], engine: str | None = None, acted: bool | None = None
    ) -> list[SignalBrief]:
        """Signals from the session. `acted=False` gives the ones passed over.

        The passed signals are the only rows that can say whether the rules are
        filtering junk or discarding winners — but a signal that fired with the
        book already full (`blocked`) was never a decision, and its outcome says
        nothing about the filter.
        """
        pack = ctx.deps.engine(engine)
        if pack is None:
            return []
        rows = [s for s in pack.signals if acted is None or s.acted == acted]
        return [SignalBrief.of(s) for s in rows[:MAX_ROWS]]

    @toolset.tool
    def window_summary(ctx: RunContext[ReviewDeps], engine: str | None = None) -> WindowSummary:
        """Per-strategy aggregates over the trailing window — the population any
        claim has to be drawn from, since one session never is. Pooled capture,
        worst MAE, early exits, and the taken-vs-passed signal split."""
        pack = ctx.deps.engine(engine)
        return window_summary_of(pack) if pack else WindowSummary(window_days=0)

    @toolset.tool
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

    return toolset


def build_agent(model: str | Model | None = None) -> Agent[ReviewDeps, AiReview]:
    """The review agent: the model, the cache settings, the bound, the tools."""
    from pydantic_ai.exceptions import UserError

    try:
        agent = Agent[ReviewDeps, AiReview](
            resolve_model(model),
            deps_type=ReviewDeps,
            output_type=AiReview,
            instructions=INSTRUCTIONS.format(
                min_trades=MIN_TRADES_FOR_FINDING, max_requests=MAX_REQUESTS
            ),
            model_settings=MODEL_SETTINGS,
            toolsets=[BoundedToolset(build_toolset())],
            retries=2,
        )
    except UserError as exc:
        raise TrdError(
            f"{exc} trd reads it from the environment: export ANTHROPIC_API_KEY, "
            "on a laptop from the agents vault "
            "(op read 'op://agents/<item>/<field>'), and in the cluster from the "
            "same secret pattern the Telegram token uses."
        ) from None

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
    result = agent.run_sync(_brief(deps), deps=deps, usage_limits=USAGE_LIMITS)
    usage = result.usage
    return AiReviewRun(
        review=result.output,
        usage=AiUsage(
            model=str(getattr(agent.model, "model_name", agent.model)),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_write_tokens", 0) or 0,
            requests=getattr(usage, "requests", 0) or 0,
        ),
    )
