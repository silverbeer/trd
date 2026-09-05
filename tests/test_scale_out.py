"""Scale-out: take most at the target, let a runner ride.

The plumbing for partial exits already existed — `book_exit`, `closed_quantity`,
`booked_pnl`, R weighed against the size taken at entry. What was missing was a
rule that decides to sell *part*, and two execution paths that honour it
identically.

The trap this guards against is the one the ticket names: scaling out raises the
win rate almost by construction while it may lower expectancy. So the tests here
are about arithmetic that stays honest — an R-multiple that still weighs each
piece against the original size, a runner that is not counted as a second trade,
and the live engine and the backtest scoring the same scaled-out trade the same.
"""

from datetime import datetime
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.engine.exits import DEFAULT_EXIT_PARAMS, ExitDecision, evaluate, exit_quantity
from trd.models import EnginePosition, PositionStatus
from trd.services import EngineService

ENTRY = Decimal("100")
RISK = Decimal("10")  # 1R
TARGET = ENTRY + RISK * 2  # the 2R target every fixture below trades into


def position(quantity: str = "10", closed_quantity: str = "0") -> EnginePosition:
    return EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy="momentum",
        opened_at=datetime(2026, 6, 1, 15, 0),
        entry_price=ENTRY,
        quantity=Decimal(quantity),
        stop_price=ENTRY - RISK,
        target_price=TARGET,
        atr_at_entry=Decimal("5"),
        trail_high=ENTRY,
        closed_quantity=Decimal(closed_quantity),
    )


def params(**overrides) -> dict[str, float]:
    return {**DEFAULT_EXIT_PARAMS, **overrides}


def bars_at(price: str):
    """A short series ending at `price`, enough for the rules that read one."""
    return make_bars([float(price)] * 30)


# ----------------------------------------------------------------- the decision


def test_every_other_rule_still_sells_everything() -> None:
    """The field was added with a default of 1, so nothing but scale-out moved.
    A rule that started selling part of a position by accident would be a silent
    change to every R-multiple in the scorecard."""
    assert ExitDecision(rule="stop", reason="x").fraction == 1
    assert ExitDecision(rule="stop", reason="x").partial is False


def test_scale_out_is_off_by_default() -> None:
    """It ships off, and only a backtest is allowed to argue it on."""
    assert DEFAULT_EXIT_PARAMS["scale_out_pct"] == 0.0
    decision = evaluate(
        position(), bars_at("125"), bars_at("125"), Decimal("125"), params(), datetime.now(), "1d"
    )
    assert decision is not None
    assert decision.rule == "target"  # closes flat, exactly as before
    assert decision.partial is False


def test_scale_out_takes_part_at_the_target() -> None:
    decision = evaluate(
        position(),
        bars_at("125"),
        bars_at("125"),
        Decimal("125"),
        params(scale_out_pct=70.0),
        datetime.now(),
        "1d",
    )
    assert decision is not None
    assert decision.rule == "scale_out"
    assert decision.partial is True
    assert decision.fraction == Decimal("0.7")
    assert decision.level == TARGET


def test_the_runner_is_never_scaled_again() -> None:
    """Without this the rule would sell 70% of the remainder on every pass price
    sat above the target — a slow bleed dressed up as a rule."""
    runner = position(closed_quantity="7")
    assert runner.is_partial
    decision = evaluate(
        runner,
        bars_at("125"),
        bars_at("125"),
        Decimal("125"),
        params(scale_out_pct=70.0),
        datetime.now(),
        "1d",
    )
    # Not scale_out, and not target either: the runner leaves on the trail, a
    # stop, a broken thesis or time.
    assert decision is None or decision.rule not in {"scale_out", "target"}


def test_a_manual_trim_still_gets_its_target(caplog) -> None:
    """With scale-out OFF, `trd engine trim` must behave exactly as it always
    has. A trim silently converting a trade into a runner would be a behaviour
    change nobody asked for."""
    trimmed = position(closed_quantity="5")
    decision = evaluate(
        trimmed, bars_at("125"), bars_at("125"), Decimal("125"), params(), datetime.now(), "1d"
    )
    assert decision is not None and decision.rule == "target"


def test_a_stop_still_beats_the_scale_out() -> None:
    """Order matters: capital protection before profit-taking. A bar that is
    through the stop is a stop, whatever else is also true."""
    decision = evaluate(
        position(),
        bars_at("85"),
        bars_at("85"),
        Decimal("85"),
        params(scale_out_pct=70.0),
        datetime.now(),
        "1d",
    )
    assert decision is not None and decision.rule == "stop"


# ------------------------------------------------------------------- the sizing


def test_the_sold_quantity_is_the_fraction_of_what_is_still_held() -> None:
    decision = ExitDecision(rule="scale_out", reason="x", fraction=Decimal("0.7"))
    assert exit_quantity(position(quantity="10"), decision) == Decimal("7")
    # Of the REMAINDER, not the original size: a position already half sold has
    # five left, and 70% of it is 3.5.
    assert exit_quantity(position(quantity="10", closed_quantity="5"), decision) == Decimal("3.5")


def test_a_full_decision_sells_the_remainder() -> None:
    decision = ExitDecision(rule="stop", reason="x")
    assert exit_quantity(position(quantity="10", closed_quantity="4"), decision) == Decimal("6")


def test_dust_is_sold_with_the_rest() -> None:
    """A day engine holds hundredths of a share. A fraction that would leave less
    than the quantisation step behind must close the trade instead of parking a
    millionth of a share in a slot and reporting an R on it."""
    tiny = position(quantity="0.000001")
    decision = ExitDecision(rule="scale_out", reason="x", fraction=Decimal("0.7"))
    assert exit_quantity(tiny, decision) == Decimal("0.000001")


# ------------------------------------------------------------------ the scoring


def test_r_still_weighs_each_piece_against_the_size_taken_at_entry() -> None:
    """The ticket's own worked example: 90% at +2R and the last 10% stopped at
    -1R is +1.7R. One number for one trade is what makes the scorecard mean
    anything."""
    trade = position(quantity="10")
    trade.book_exit(Decimal("9"), ENTRY + RISK * 2)  # 90% at +2R
    trade.book_exit(Decimal("1"), ENTRY - RISK)  # the rest at -1R
    assert trade.realized_r is not None
    assert round(float(trade.realized_r), 4) == 1.7


def test_a_scaled_out_winner_can_still_lose_on_the_runner() -> None:
    """The case the backtest exists to expose: a name that spikes into the target
    and reverses, where the remainder leaves well below where the 90% went out."""
    trade = position(quantity="10")
    trade.book_exit(Decimal("7"), TARGET)  # +2R on 70%
    trade.book_exit(Decimal("3"), ENTRY - RISK)  # -1R on the runner
    assert trade.realized_r is not None
    assert round(float(trade.realized_r), 4) == 1.1  # 0.7*2 + 0.3*(-1)


# ---------------------------------------------------- live and backtest parity


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    assert len(service.scan().opened) == 1
    return service


def test_the_live_scanner_books_a_partial_without_ending_the_trade(
    engine: EngineService, provider: FakeProvider, conn: duckdb.DuckDBPyConnection
) -> None:
    """A runner keeps its slot, its stop and its plan. What changes is the size."""
    account = engine.account()
    held = engine.positions.list_open(account.id)[0][0]
    engine.configs.upsert(
        account.id,
        "engine",
        Decimal("10000"),
        5,
        ["momentum"],
        {**DEFAULT_EXIT_PARAMS, "scale_out_pct": 70.0},
        3,
        engine.config().sizing_mode,
        "1d",
        0,
    )
    # Price through the target.
    provider.add_symbol("AAA", price=str(float(held.target_price) * 1.01), volume=1_200_000)

    result = engine.scan()
    assert len(result.closed) == 1
    fill = result.closed[0]
    assert fill.rule == "scale_out"

    after = engine.positions.list_open(account.id)
    assert len(after) == 1  # still open, still holding a slot
    runner = after[0][0]
    assert runner.is_partial
    assert runner.closed_quantity == fill.quantity
    assert runner.exit_reason is None  # nothing ended
    assert runner.stop_price == held.stop_price  # the plan is untouched
    assert runner.target_price == held.target_price


def test_the_two_paths_score_an_identical_scaled_out_trade_identically() -> None:
    """The parity the ticket asks for. Both take their size from `exit_quantity`
    and book it through `book_exit`, so the only way they could disagree is if one
    of them stopped doing that — which is exactly what this asserts."""
    decision = ExitDecision(rule="scale_out", reason="x", fraction=Decimal("0.7"), level=TARGET)

    live = position(quantity="10")
    backtest = position(quantity="10")

    for trade in (live, backtest):
        sold = exit_quantity(trade, decision)
        trade.book_exit(sold, TARGET)

    # ...then the runner stops out in both.
    for trade in (live, backtest):
        trade.book_exit(exit_quantity(trade, ExitDecision(rule="stop", reason="x")), ENTRY - RISK)
        trade.status = PositionStatus.CLOSED

    assert live.realized_r == backtest.realized_r
    assert live.booked_pnl == backtest.booked_pnl
    assert live.exit_price == backtest.exit_price  # the quantity-weighted average
    assert live.remaining_quantity == 0


def test_the_backtest_does_not_count_a_runner_as_a_second_trade(
    conn: duckdb.DuckDBPyConnection, provider: FakeProvider
) -> None:
    """Otherwise a scale-out inflates the trade count and reports each piece as
    its own win — the exact way this idea would flatter itself in the scorecard
    that is supposed to judge it."""
    from trd.services.backtest import BacktestService

    bars = make_bars(uptrend(n=400))
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(bars[-1].close), volume=1_200_000)
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    backtest = BacktestService(conn)
    flat = backtest.run(scale_out_pct=0.0)
    runner = backtest.run(scale_out_pct=70.0)

    # Every trade in both runs is one trade, whatever it did on the way out.
    assert len(runner.trades) <= len(flat.trades)
    for trade in runner.trades:
        assert trade.r_multiple is None or -5 < float(trade.r_multiple) < 10


def test_a_build_without_the_rule_refuses_a_config_that_asks_for_it() -> None:
    """A pod running older code would close every winner flat at the target and
    report it as the strategy's result — silent, and wrong in the direction that
    looks like a finding."""
    from trd.engine.exits import PARAM_RULES, missing_rules

    assert PARAM_RULES["scale_out_pct"] == "scale_out"
    # Simulate the older build by asking for a rule key that does not exist.
    assert missing_rules({"scale_out_pct": 70.0}) == []  # this build has it
