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
    agent = build_agent(model=TestModel())
    names = set(agent._function_toolset.tools)
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
