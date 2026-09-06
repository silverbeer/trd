"""The review agent: the only part of trd that talks to a model.

Nothing here calls a provider — `ALLOW_MODEL_REQUESTS = False` makes that a
property of the test run rather than a habit, and every test drives the agent
through pydantic-ai's own test models.

What is worth testing about an agent is not its prose. It is the shape of what
surrounds it: that the tools are read-only, that the output is validated, that a
provider failure costs the commentary and not the review, and that a pack with a
planted pattern in it produces a finding naming that pattern — because if the
model cannot find the planted one, the prompt is wrong and no amount of good
output on a real day would tell you.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

pytest.importorskip("pydantic_ai", reason="the review agent lives behind the 'ai' extra")

from pydantic_ai import models
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.agents.review_agent import (
    DEFAULT_MODEL,
    AiReview,
    AiUsage,
    ReviewDeps,
    build_agent,
    build_toolset,
    run_ai_review,
)
from trd.cli.app import app
from trd.models import TradeOutcome
from trd.services.review import (
    ConfigSummary,
    EnginePack,
    ReviewPack,
    WindowTrade,
    review,
)

# No test may reach a provider. Set once, for the module, so a future test that
# forgets to override the model fails loudly instead of spending money.
models.ALLOW_MODEL_REQUESTS = False

runner = CliRunner()
ON = date(2026, 9, 3)


def planted_pack(strategy: str = "breakout", n: int = 40) -> ReviewPack:
    """A pack with one obvious, deliberate pattern: a strategy that is offered
    2R a trade and keeps a tenth of it."""
    from trd.services.daily_report import EngineDay

    trades = [
        WindowTrade(
            position_id=i,
            strategy=strategy,
            rule="stop",
            closed_on=ON - timedelta(days=i % 20),
            outcome=TradeOutcome(
                position_id=i,
                computed_at=datetime(2026, 9, 3, 16, 0),
                timeframe="1d",
                bars_seen=10,
                risk_per_share=Decimal("10"),
                mae_r=Decimal("-0.8"),
                mfe_r=Decimal("2.0"),
                mfe_bar=8,
                exit_r=Decimal("0.2"),
                capture=Decimal("0.1"),
                follow_through_r=Decimal("0.1"),
                follow_through_bars=5,
                follow_through_seen=5,
            ),
        )
        for i in range(1, n + 1)
    ]
    pack = EnginePack(
        engine="swing",
        on=ON,
        config=ConfigSummary(
            account="engine-sim",
            timeframe="1d",
            position_size=Decimal("100"),
            sizing_mode="exposure",
            max_positions=10,
            max_entries_per_day=0,
            earnings_blackout_days=3,
            strategies=[strategy],
        ),
        day=EngineDay(engine="swing", last_session=ON),
        window_days=30,
        window_trades=trades,
    )
    return ReviewPack(on=ON, generated_at=datetime.now(), build="test", engines=[pack])


# ------------------------------------------------------------------ the surface


def test_every_tool_is_a_read() -> None:
    """Read-only is a property of the tool surface, not of the instructions. A
    model cannot move a stop it has no tool for, however it is prompted."""
    names = set(build_toolset().tools)
    assert names == {
        "deterministic_findings",
        "trades_closed",
        "signals_fired",
        "window_summary",
        "rule_book",
    }
    # Whole words, not substrings: "deterministic_findings" contains "rm", which
    # is how a naive check on this would fail on a perfectly innocent read.
    mutating = {"add", "rm", "trim", "sell", "buy", "set", "close", "open", "place", "cancel"}
    words = {word for name in names for word in name.split("_")}
    assert not words & mutating


def test_the_default_model_is_named_and_overridable() -> None:
    """Configuration, not code — but a default that quietly changed would make
    two days' reviews incomparable without saying so."""
    assert DEFAULT_MODEL == "anthropic:claude-opus-5"
    agent = build_agent(model=TestModel())
    assert isinstance(agent.model, TestModel)


def test_output_is_validated_not_trusted() -> None:
    """The whole argument for a typed agent: a model that drifts fails loudly
    rather than emitting plausible prose that reads fine and is wrong."""

    def drifts(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Looks good to me!")])

    agent = build_agent(model=FunctionModel(drifts))
    pack = planted_pack()
    with pytest.raises(Exception):  # noqa: B017 - any failure is acceptable; silence is not
        run_ai_review(pack, review(pack.engines, ON), agent=agent)


# -------------------------------------------------------------- the tool calls


def test_the_agent_can_read_the_pack_through_its_tools() -> None:
    """TestModel calls every tool it is given, so this exercises each schema —
    the failure it catches is a tool whose pydantic return type cannot be
    serialized for the model at all."""
    agent = build_agent(model=TestModel())
    pack = planted_pack()
    run = run_ai_review(pack, review(pack.engines, ON), agent=agent)
    assert isinstance(run.review, AiReview)
    assert run.usage.requests >= 1


def test_a_planted_pattern_reaches_the_model() -> None:
    """If the model cannot find a pattern that was put there on purpose, the
    prompt is wrong — and no amount of plausible output on a real day would say
    so. This asserts the pattern is in what the model is handed, and that what it
    concludes comes back typed.
    """
    seen: dict[str, object] = {}

    def reads_then_answers(messages, info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("deterministic_findings", {})])
        seen["tool_output"] = messages[-1].parts[0].content
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "summary": "breakout finds moves and gives them back.",
                        "nothing_conclusive": False,
                        "findings": [
                            {
                                "headline": "breakout keeps a tenth of what it is offered",
                                "engine": "swing",
                                "scope": "strategy",
                                "subject": "breakout",
                                "rationale": "Capture is 0.1 across the window.",
                                "rests_on": ["capture 0.10 over 40 trades"],
                                "trades": 40,
                                "hypothesis": False,
                                "test": "trd engine backtest --years 5",
                            }
                        ],
                        "watch_next": ["whether capture recovers with a trailing exit"],
                    },
                )
            ]
        )

    pack = planted_pack()
    computed = review(pack.engines, ON)
    run = run_ai_review(pack, computed, agent=build_agent(model=FunctionModel(reads_then_answers)))

    # The planted pattern was in what the model was given...
    assert "capture" in str(seen["tool_output"]).lower()
    # ...and what it concluded came back as a validated object, not prose.
    assert run.review.findings[0].subject == "breakout"
    assert run.review.findings[0].hypothesis is False
    assert run.review.findings[0].test


def test_nothing_conclusive_survives_the_round_trip() -> None:
    """The answer that must remain available to the model. A schema that made it
    awkward to say nothing would quietly select for manufactured findings."""

    def quiet(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "summary": "Five trades. Nothing separable from noise.",
                        "nothing_conclusive": True,
                        "findings": [],
                        "watch_next": [],
                    },
                )
            ]
        )

    pack = planted_pack(n=5)
    run = run_ai_review(
        pack, review(pack.engines, ON), agent=build_agent(model=FunctionModel(quiet))
    )
    assert run.review.nothing_conclusive is True
    assert run.review.findings == []


# ----------------------------------------------------------------------- cost


def test_cost_is_reported_only_when_prices_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A price table in source is the kind of fact that goes stale silently, and
    a review that understates its own cost is the last thing this should do."""
    usage = AiUsage(model="anthropic:claude-opus-5", input_tokens=1_000_000, output_tokens=100_000)
    monkeypatch.delenv("TRD_AI_PRICE_IN", raising=False)
    monkeypatch.delenv("TRD_AI_PRICE_OUT", raising=False)
    assert usage.cost_usd is None

    monkeypatch.setenv("TRD_AI_PRICE_IN", "5")
    monkeypatch.setenv("TRD_AI_PRICE_OUT", "25")
    assert usage.cost_usd == Decimal("7.5")  # 1M in at $5, 100k out at $25


def test_a_broken_price_setting_reports_no_cost_rather_than_a_wrong_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRD_AI_PRICE_IN", "not-a-number")
    monkeypatch.setenv("TRD_AI_PRICE_OUT", "25")
    assert AiUsage(model="m", input_tokens=10, output_tokens=10).cost_usd is None


# ------------------------------------------------------------------- the deps


def test_the_agent_sees_the_pack_and_nothing_else() -> None:
    """Its whole world. No database handle, no provider, no filesystem."""
    pack = planted_pack()
    deps = ReviewDeps(pack=pack, computed=review(pack.engines, ON))
    assert set(vars(deps)) == {"pack", "computed"}
    assert deps.engine("swing") is not None
    assert deps.engine("nope") is None
    assert deps.engine(None) is pack.engines[0]  # single-engine default


# ----------------------------------------------------------------------- CLI


def test_cli_without_the_extra_keeps_the_deterministic_review(
    cli_env: FakeProvider, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing optional dependency must cost the commentary and nothing else.
    The arithmetic is the review; the model is a remark about it."""
    import builtins

    from trd.db.connection import connect
    from trd.services import EngineService
    from trd.services.outcomes import OutcomeService

    home = tmp_path / "swing"
    home.mkdir()
    conn = connect(home / "trd.duckdb")
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    cli_env.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    service = EngineService(conn, cli_env)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    service.scan()
    trade = service.position_rows()[0].position
    service.positions.close(
        trade.id,
        datetime.combine(ON, datetime.min.time()).replace(hour=16),
        trade.stop_price,
        "stopped",
        "stop",
    )
    OutcomeService(conn).backfill()
    conn.close()

    real_import = builtins.__import__

    def no_agent(name: str, *args, **kwargs):
        if name.startswith("trd.agents"):
            raise ImportError("pydantic_ai is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_agent)
    result = runner.invoke(
        app,
        ["engine", "review", "--engines", f"swing={home}", "--date", ON.isoformat(), "--ai"],
    )
    assert result.exit_code == 0, result.output
    assert "uv sync --extra ai" in result.output
    assert "Nothing conclusive" in result.output  # the review itself still ran


# ------------------------------------------------------------------ the seam


def test_provider_strings_pass_straight_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """pydantic-ai's own `provider:model` strings are the seam: thirty-odd
    providers for free, and trd never learns which one it got."""
    from trd.agents.review_agent import resolve_model

    assert resolve_model("anthropic:claude-opus-5") == "anthropic:claude-opus-5"
    assert resolve_model("xai:grok-4") == "xai:grok-4"
    monkeypatch.setenv("TRD_AI_MODEL", "  openai:gpt-5  ")
    assert resolve_model(None) == "openai:gpt-5"
    monkeypatch.delenv("TRD_AI_MODEL")
    assert resolve_model(None) == DEFAULT_MODEL
    model = TestModel()
    assert resolve_model(model) is model


def test_claude_code_scheme_resolves_to_the_subscription_adapter() -> None:
    from trd.agents.claude_code import ClaudeCodeModel
    from trd.agents.review_agent import resolve_model

    model = resolve_model("claude-code:sonnet")
    assert isinstance(model, ClaudeCodeModel)
    assert model.model_name == "claude-code:sonnet"
    default = resolve_model("claude-code")  # a bare scheme gets the default alias
    assert isinstance(default, ClaudeCodeModel) and default.model_name == "claude-code:opus"


def test_the_scheme_resolves_from_the_environment_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """The nightly sets TRD_AI_MODEL and passes nothing; the first version
    resolved the scheme only for an explicit argument and ran the env string
    straight into pydantic-ai, which had never heard of it."""
    from trd.agents.claude_code import ClaudeCodeModel
    from trd.agents.review_agent import resolve_model

    monkeypatch.setenv("TRD_AI_MODEL", "claude-code:sonnet")
    assert isinstance(resolve_model(None), ClaudeCodeModel)


def test_vertex_scheme_needs_a_project_and_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A half-configured backend fails at build time with the variable named,
    not at 02:00 in a cluster with a stack trace."""
    from trd.agents.review_agent import resolve_model
    from trd.errors import TrdError

    pytest.importorskip("google.auth", reason="the vertex extra is not installed")
    monkeypatch.delenv("TRD_AI_VERTEX_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(TrdError, match="TRD_AI_VERTEX_PROJECT"):
        resolve_model("vertex:claude-opus-5")
    monkeypatch.setenv("TRD_AI_VERTEX_PROJECT", "proj")
    with pytest.raises(TrdError, match="model name"):
        resolve_model("vertex:")


# ------------------------------------------------------------ the claude-code adapter


def canned_claude_code(replies: list[dict]) -> Any:
    """A ClaudeCodeModel whose binary is a list of reports, consumed in order.
    Records the system prompt and transcript of every turn."""
    from trd.agents.claude_code import ClaudeCodeModel

    class Canned(ClaudeCodeModel):
        turns: ClassVar[list[tuple[str, str, dict]]] = []

        async def _run(self, system: str, prompt: str, schema: dict) -> dict:
            self.turns.append((system, prompt, schema))
            return replies[len(self.turns) - 1]

    return Canned("opus")


def test_claude_code_adapter_drives_the_tool_loop_and_answers_typed() -> None:
    """The whole point of the adapter: pydantic-ai's loop, trd's tools, and a
    validated answer — with the binary emulated so the test stays offline."""
    answer = {
        "summary": "breakout finds moves and gives them back.",
        "nothing_conclusive": False,
        "findings": [],
        "watch_next": [],
    }
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 40}
    model = canned_claude_code(
        [
            {
                "structured_output": {"calls": [{"tool": "trades_closed", "args": {}}]},
                "usage": usage,
            },
            {
                "structured_output": {"calls": [{"tool": "final_result", "args": answer}]},
                "usage": usage,
            },
        ]
    )
    pack = planted_pack()
    run = run_ai_review(pack, review(pack.engines, ON), agent=build_agent(model=model))
    assert run.review.summary == answer["summary"]
    assert run.usage.model == "claude-code:opus"
    assert run.usage.requests == 2
    assert run.usage.cache_read_tokens == 80
    assert run.usage.input_tokens == 280  # the binary's input excludes cache lanes; ours includes

    system, _, schema = model.turns[0]
    # Instructions and the tool catalogue travel in the system prompt...
    assert "mechanical trading engine" in system
    assert "### trades_closed" in system and "### final_result" in system
    # ...the reply is constrained to known tools...
    assert set(schema["properties"]["calls"]["items"]["properties"]["tool"]["enum"]) >= {
        "trades_closed",
        "final_result",
    }
    # ...and the second turn re-renders the transcript with the tool result in it.
    _, second_prompt, _ = model.turns[1]
    assert "[you called trades_closed" in second_prompt
    assert "[result of trades_closed" in second_prompt
    assert "Session under review" in second_prompt


def test_claude_code_adapter_never_hands_the_child_an_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """With ANTHROPIC_API_KEY in its environment the binary bills the key,
    silently — the exact cost this adapter exists to avoid."""
    import asyncio

    from trd.agents.claude_code import ClaudeCodeModel

    fake = tmp_path / "claude"
    fake.write_text(
        "#!/bin/sh\n"
        'printf \'{"structured_output":{"calls":[]},"result":"%s","usage":{}}\' '
        '"key=${ANTHROPIC_API_KEY:-absent} nested=${CLAUDECODE:-absent}"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    model = ClaudeCodeModel("opus", binary=str(fake))
    report = asyncio.run(model._run("sys", "prompt", {"type": "object"}))
    assert report["result"] == "key=absent nested=absent"


def test_claude_code_adapter_reports_a_missing_binary_plainly() -> None:
    import asyncio

    from trd.agents.claude_code import ClaudeCodeModel
    from trd.errors import TrdError

    with pytest.raises(TrdError, match="not on PATH"):
        asyncio.run(ClaudeCodeModel("opus", binary="no-such-claude")._run("s", "p", {}))


# ------------------------------------------------------------------ the bound


def test_the_round_trips_are_bounded_and_the_bound_still_answers() -> None:
    """A model that would call tools forever is made to answer instead: past
    the last permitted turn its calls come back refused, with the tool set —
    and so the cached prefix — untouched. The night's review survives its own
    ceiling."""
    from trd.agents.review_agent import MAX_REQUESTS

    seen: list[tuple[int, str]] = []

    def greedy(messages, info: AgentInfo) -> ModelResponse:
        last = messages[-1].parts[-1]
        returned = str(getattr(last, "content", ""))
        seen.append((len(info.function_tools), returned))
        if "No turns left" not in returned:
            return ModelResponse(parts=[ToolCallPart("rule_book", {})])
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"summary": "Out of turns.", "nothing_conclusive": True, "findings": []},
                )
            ]
        )

    pack = planted_pack()
    run = run_ai_review(
        pack, review(pack.engines, ON), agent=build_agent(model=FunctionModel(greedy))
    )
    assert run.usage.requests == MAX_REQUESTS + 1
    assert all(n > 0 for n, _ in seen)  # the tools were never withdrawn
    assert "No turns left" in seen[-1][1]
    assert run.review.nothing_conclusive is True


# ------------------------------------------------------------- the payloads


def test_tool_payloads_carry_what_a_judgement_uses_and_no_more() -> None:
    """The first version returned the storage model whole and cost $1.38 a run.
    A trade row is symbol, strategy, rule, R, MAE, MFE, capture, follow-through
    and the entry reason — not prices, quantities, timestamps, or eight places."""
    from trd.agents.review_agent import SignalBrief, TradeBrief, WindowSummary

    trade_fields = set(TradeBrief.model_fields)
    assert trade_fields == {
        "symbol",
        "strategy",
        "rule",
        "bars_held",
        "r",
        "mae_r",
        "mfe_r",
        "capture",
        "after_r",
        "entry_reason",
    }
    assert not trade_fields & {"entry_price", "exit_price", "quantity", "opened_at", "closed_at"}
    assert set(SignalBrief.model_fields) == {
        "symbol",
        "strategy",
        "acted",
        "blocked",
        "score",
        "resolution",
        "forward_r",
        "mfe_r",
        "mae_r",
    }
    assert set(WindowSummary.model_fields) == {"window_days", "by_strategy", "signals"}


def test_projections_round_to_two_places() -> None:
    from trd.agents.review_agent import TradeBrief
    from trd.services.review import TradeReview

    trade = TradeReview(
        symbol="AAA",
        strategy="breakout",
        strategy_name="Breakout",
        strategy_intent="x",
        opened_at=datetime(2026, 9, 1, 9, 30),
        entry_price=Decimal("100.12345678"),
        quantity=Decimal("10"),
        stop_price=Decimal("98"),
        target_price=Decimal("104"),
        r_multiple=Decimal("0.123456789"),
        outcome=TradeOutcome(
            position_id=1,
            computed_at=datetime(2026, 9, 3, 16, 0),
            timeframe="1d",
            bars_seen=3,
            risk_per_share=Decimal("2"),
            mae_r=Decimal("-0.987654321"),
            mfe_r=Decimal("1.5"),
            exit_r=Decimal("0.123456789"),
            capture=Decimal("0.0823045"),
            follow_through_r=Decimal("0.7"),
            follow_through_seen=0,  # no future yet: must read as unknown, not 0.7
        ),
    )
    brief = TradeBrief.of(trade)
    assert brief.r == Decimal("0.12")
    assert brief.mae_r == Decimal("-0.99")
    assert brief.capture == Decimal("0.08")
    assert brief.after_r is None


def test_the_brief_carries_the_findings_and_the_window() -> None:
    """Two round trips the model made every night — fetching the findings and
    the per-strategy window — are folded into the prompt it starts with."""
    from trd.agents.review_agent import _brief

    pack = planted_pack()
    deps = ReviewDeps(pack=pack, computed=review(pack.engines, ON))
    brief = _brief(deps)
    assert "swing/breakout over 30d: 40 trades" in brief
    assert "capture 10%" in brief
    assert "FINDING swing/strategy breakout (n=40)" in brief


# ----------------------------------------------------------- cache accounting


def test_cache_lanes_are_priced_and_uncached_input_is_what_is_left(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """input_tokens is the provider's total; the cache lanes are the part of it
    that went through the cache. Read at a tenth, written at a quarter more."""
    monkeypatch.setenv("TRD_AI_PRICE_IN", "5")
    monkeypatch.setenv("TRD_AI_PRICE_OUT", "25")
    monkeypatch.delenv("TRD_AI_PRICE_CACHE_READ", raising=False)
    monkeypatch.delenv("TRD_AI_PRICE_CACHE_WRITE", raising=False)
    usage = AiUsage(
        model="m",
        input_tokens=1_000_000,
        cache_read_tokens=800_000,
        cache_write_tokens=100_000,
        output_tokens=0,
    )
    assert usage.uncached_input_tokens == 100_000
    # 100k at $5 + 800k at $0.50 + 100k at $6.25 = 0.5 + 0.4 + 0.625
    assert usage.cost_usd == Decimal("1.525")

    monkeypatch.setenv("TRD_AI_PRICE_CACHE_READ", "0.25")  # a model with a different read rate
    assert usage.cost_usd == Decimal("1.325")


# -------------------------------------------------------------- the render


def test_the_render_prints_the_cache_split_not_a_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run reading nothing from the cache is paying full price for its own
    history whatever the settings claim; the line that would show it must not
    be folded into one input number."""
    from rich.console import Console

    from trd.agents.review_agent import AiReviewRun
    from trd.cli.render import ai_review_renderables

    monkeypatch.setenv("TRD_AI_PRICE_IN", "5")
    monkeypatch.setenv("TRD_AI_PRICE_OUT", "25")
    run = AiReviewRun(
        review=AiReview(summary="Quiet.", nothing_conclusive=True),
        usage=AiUsage(
            model="anthropic:claude-opus-5",
            input_tokens=50_000,
            cache_read_tokens=40_000,
            cache_write_tokens=9_000,
            output_tokens=3_000,
            requests=4,
        ),
    )
    console = Console(record=True, width=200)
    for renderable in ai_review_renderables(run):
        console.print(renderable, markup=True, highlight=False)
    text = console.export_text()
    assert "40,000 cached" in text and "9,000 written to cache" in text
    assert "4 request(s)" in text
    # 1k at $5 + 40k at $0.50 + 9k at $6.25 + 3k at $25 = 0.005 + 0.02 + 0.05625 + 0.075
    assert "$0.1562" in text
