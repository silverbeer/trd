"""The decision review: the pack, and the findings that need no model.

What is being guarded here is mostly restraint. The arithmetic is simple; the
hard part is a reviewer that refuses to call five trades a pattern, refuses to
use tomorrow's data to review yesterday, and is willing to say nothing happened.
A reviewer that finds something every day is fitting noise, and these tests are
what stop this one from drifting into that.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.cli.app import app
from trd.models import TradeOutcome
from trd.services import EngineService
from trd.services.outcomes import OutcomeService
from trd.services.review import (
    LOW_CAPTURE,
    MIN_TRADES_FOR_FINDING,
    MIN_TRADES_TO_SPEAK,
    ConfigSummary,
    EnginePack,
    WindowSignal,
    WindowTrade,
    capture_findings,
    engine_pack,
    filter_findings,
    review,
    slow_exit_findings,
    stop_findings,
)

runner = CliRunner()
ON = date(2026, 9, 3)


def outcome(
    position_id: int,
    mfe: str = "2.0",
    exit_r: str = "0.2",
    mae: str = "-0.9",
    mfe_bar: int = 2,
    bars: int = 10,
    follow: str | None = None,
) -> TradeOutcome:
    """A measured trade whose every number is chosen, not sampled."""
    mfe_r = Decimal(mfe)
    return TradeOutcome(
        position_id=position_id,
        computed_at=datetime(2026, 9, 3, 16, 0),
        timeframe="1d",
        bars_seen=bars,
        risk_per_share=Decimal("10"),
        mae_r=Decimal(mae),
        mae_bar=1,
        mfe_r=mfe_r,
        mfe_bar=mfe_bar,
        exit_r=Decimal(exit_r),
        capture=(Decimal(exit_r) / mfe_r) if mfe_r > 0 else None,
        follow_through_r=Decimal(follow) if follow is not None else None,
        follow_through_bars=5,
        follow_through_seen=5 if follow is not None else 0,
    )


def pack(
    trades: list[WindowTrade] | None = None,
    signals: list[WindowSignal] | None = None,
    engine: str = "swing",
) -> EnginePack:
    from trd.services.daily_report import EngineDay

    return EnginePack(
        engine=engine,
        on=ON,
        config=ConfigSummary(
            account="engine-sim",
            timeframe="1d",
            position_size=Decimal("100"),
            sizing_mode="exposure",
            max_positions=10,
            max_entries_per_day=0,
            earnings_blackout_days=3,
            strategies=["momentum"],
        ),
        day=EngineDay(engine=engine, last_session=ON),
        window_days=30,
        window_trades=trades or [],
        window_signals=signals or [],
    )


def window(strategy: str, n: int, rule: str | None = "stop", **kw) -> list[WindowTrade]:
    return [
        WindowTrade(
            position_id=i,
            strategy=strategy,
            rule=rule,
            closed_on=ON - timedelta(days=i % 20),
            outcome=outcome(i, **kw),
        )
        for i in range(1, n + 1)
    ]


# ------------------------------------------------------------ refusing to speak


def test_a_handful_of_trades_is_not_a_population() -> None:
    """Four trades agreeing is a coincidence with a narrative attached. The
    detector must be silent, not quieter."""
    thin = window("momentum", MIN_TRADES_TO_SPEAK - 1, mfe="2.0", exit_r="0.1")
    assert capture_findings(pack(thin)) == []


def test_a_small_sample_is_labelled_a_hypothesis_and_carries_its_test() -> None:
    """Real enough to say out loud, not enough to act on. A hypothesis that reads
    like a finding is how a backlog fills with work nobody should do."""
    small = window("momentum", MIN_TRADES_FOR_FINDING - 1, mfe="2.0", exit_r="0.1")
    found = capture_findings(pack(small))
    assert len(found) == 1
    assert found[0].hypothesis is True
    assert found[0].test  # names the backtest that would settle it

    big = window("momentum", MIN_TRADES_FOR_FINDING, mfe="2.0", exit_r="0.1")
    assert capture_findings(pack(big))[0].hypothesis is False


def test_nothing_conclusive_is_a_real_answer() -> None:
    """The expected output on most days. A reviewer whose quiet share drops to
    zero has stopped reviewing and started performing."""
    # Healthy on every dimension the detectors look at: it keeps 90% of what it
    # is offered, peaks late in the hold, and its winners actually use the stop's
    # room — a shallower MAE here would (correctly) report the stop as too wide.
    healthy = window("momentum", 40, mfe="2.0", exit_r="1.8", mae="-0.8", mfe_bar=9)
    result = review([pack(healthy)], ON)
    assert result.quiet is True
    assert result.findings == []


# ------------------------------------------------------------------- detectors


def test_low_capture_points_at_the_exits_not_the_entries() -> None:
    """The finding the live swing engine actually has: the entries find moves and
    the exits are not paid for them."""
    poor = window("breakout", 30, mfe="2.0", exit_r="0.1")
    found = capture_findings(pack(poor))
    assert len(found) == 1
    assert found[0].scope == "strategy"
    assert found[0].subject == "breakout"
    assert Decimal(found[0].evidence["capture"].rstrip("%")) / 100 < LOW_CAPTURE
    assert found[0].n == 30


def test_capture_is_pooled_so_one_tiny_denominator_cannot_invent_a_finding() -> None:
    """The -507% artefact, guarded at the level that matters: a strategy that
    keeps most of what it is offered must not be reported as failing because one
    trade peaked at nothing and lost."""
    good = window("breakout", 20, mfe="2.0", exit_r="1.6")
    good.append(
        WindowTrade(
            position_id=999,
            strategy="breakout",
            rule="stop",
            closed_on=ON,
            outcome=outcome(999, mfe="0.02", exit_r="-1.0"),
        )
    )
    assert capture_findings(pack(good)) == []


def test_an_early_peak_indicts_the_exit_rather_than_the_entry() -> None:
    """The move the rule was looking for did happen. Being carried well past it
    is a slow exit, which is a different fix from a wrong entry."""
    slow = window("pullback", 25, mfe="2.0", exit_r="1.9", mfe_bar=2, bars=20)
    found = slow_exit_findings(pack(slow))
    assert len(found) == 1
    assert "peaks" in found[0].headline
    assert found[0].test and "backtest" in found[0].test


def test_a_stop_no_winner_ever_approaches_is_a_finding_about_sizing() -> None:
    winners = window("momentum", 25, mfe="2.0", exit_r="1.5", mae="-0.1")
    found = stop_findings(pack(winners))
    assert len(found) == 1
    assert "heat" in found[0].headline
    # And it must not claim the tighter stop is free.
    assert "recovered" in found[0].detail


# ------------------------------------------------------- the passed signals


def signals(taken: int, taken_hits: int, passed: int, passed_hits: int, blocked: int = 0):
    from trd.models import SignalOutcome

    rows: list[WindowSignal] = []

    def add(acted: bool, hits: int, total: int, capacity_blocked: bool) -> None:
        for i in range(total):
            rows.append(
                WindowSignal(
                    strategy="momentum",
                    fired_on=ON,
                    outcome=SignalOutcome(
                        signal_id=len(rows) + 1,
                        computed_at=datetime(2026, 9, 3),
                        timeframe="1d",
                        acted=acted,
                        capacity_blocked=capacity_blocked,
                        horizon_bars=5,
                        bars_seen=5,
                        resolution="target" if i < hits else "stop",
                    ),
                )
            )

    add(True, taken_hits, taken, False)
    add(False, passed_hits, passed, False)
    add(False, passed, blocked, True)  # blocked ones, all winners, to prove exclusion
    return rows


def test_the_filter_is_measured_against_signals_that_could_have_been_taken() -> None:
    """Signals that fired with the book full were never a decision. Counting them
    would make a capacity limit look like a judgement — and it is the difference
    between "our ranking works" and "we were busy"."""
    rows = signals(taken=100, taken_hits=30, passed=100, passed_hits=10, blocked=500)
    found = filter_findings(pack(signals=rows))
    assert len(found) == 1
    assert found[0].key == "filter.edge"
    assert "30% of 100" in found[0].evidence["taken_2r_first"]
    assert "10% of 100" in found[0].evidence["passed_takeable_2r_first"]


def test_the_uncomfortable_direction_is_reported_too() -> None:
    """The whole reason for looking. A filter that discards better trades than it
    keeps must produce a finding, not silence."""
    rows = signals(taken=100, taken_hits=5, passed=100, passed_hits=25)
    found = filter_findings(pack(signals=rows))
    assert len(found) == 1
    assert found[0].key == "filter.discards"
    assert "SKIP" in found[0].headline


def test_a_thin_side_cannot_carry_the_comparison() -> None:
    """Observed on the live swing engine at a 2-point threshold: 2% of 40 against
    0% of 14 was reported as a finding, and it was one trade."""
    assert filter_findings(pack(signals=signals(40, 1, 14, 0))) == []


def test_a_comparison_is_only_as_strong_as_its_smaller_side() -> None:
    """n is the weaker population, never the sum: 2,000 passed signals must not
    dress up a conclusion resting on thirty taken ones."""
    found = filter_findings(
        pack(signals=signals(taken=31, taken_hits=20, passed=900, passed_hits=90))
    )
    assert len(found) == 1
    assert found[0].n == 31
    assert found[0].hypothesis is False  # 31 >= the finding threshold


# ------------------------------------------------------------------- the pack


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    assert len(service.scan().opened) == 1
    return service


def test_the_pack_quotes_the_rule_rather_than_re_deriving_it(
    engine: EngineService, conn: duckdb.DuckDBPyConnection
) -> None:
    """A judgement about a decision has to be made against what the rule claimed
    it would do, and that prose lives with the rule."""
    trade = engine.position_rows()[0].position
    at = datetime.combine(ON, datetime.min.time()).replace(hour=16)
    engine.positions.close(trade.id, at, trade.stop_price, "hit the stop at 90", "stop")

    built = engine_pack(engine, OutcomeService(conn), "swing", ON)
    assert len(built.trades) == 1
    reviewed = built.trades[0]
    assert reviewed.rule == "stop"
    assert reviewed.rule_name and reviewed.rule_intent  # the registry's own words
    assert reviewed.strategy_intent
    assert reviewed.exit_reason == "hit the stop at 90"
    # The entry reason is the one recorded when the signal fired, not a fresh
    # evaluation of today's bars.
    assert reviewed.entry_reason


def test_a_review_of_a_past_date_never_uses_what_happened_after_it(
    engine: EngineService, conn: duckdb.DuckDBPyConnection
) -> None:
    """Otherwise a review is unfalsifiable, and scoring old reviews against what
    followed — the reason they are stored at all — becomes meaningless."""
    trade = engine.position_rows()[0].position
    later = datetime.combine(ON + timedelta(days=5), datetime.min.time())
    engine.positions.close(trade.id, later, trade.stop_price, "stopped", "stop")
    OutcomeService(conn).backfill()

    built = engine_pack(engine, OutcomeService(conn), "swing", ON)
    assert built.trades == []
    assert built.window_trades == []


def test_the_pack_states_what_it_could_not_measure(
    engine: EngineService, conn: duckdb.DuckDBPyConnection
) -> None:
    """An unmeasured trade silently missing from every population is how an
    average starts describing a different set of trades than the reader thinks."""
    trade = engine.position_rows()[0].position
    at = datetime.combine(ON, datetime.min.time()).replace(hour=16)
    engine.positions.close(trade.id, at, trade.stop_price, "stopped", "stop")

    built = engine_pack(engine, OutcomeService(conn), "swing", ON)
    assert built.unmeasured == 1
    assert any("no outcome measured yet" in c for c in built.caveats)


# --------------------------------------------------------------- read-only


def test_the_review_runs_against_a_read_only_database(
    engine: EngineService, conn: duckdb.DuckDBPyConnection, tmp_path
) -> None:
    """Proven, not asserted. The review reaches into both engines' databases and
    a review is never a reason to write to one — so it must work on a connection
    that physically cannot.
    """
    trade = engine.position_rows()[0].position
    at = datetime.combine(ON, datetime.min.time()).replace(hour=16)
    engine.positions.close(trade.id, at, trade.stop_price, "stopped", "stop")
    OutcomeService(conn).backfill()
    conn.close()

    from trd.db.connection import connect_read_only

    ro = connect_read_only(tmp_path / "test.duckdb")
    try:
        built = engine_pack(EngineService(ro, FakeProvider()), OutcomeService(ro), "swing", ON)
        result = review([built], ON)
    finally:
        ro.close()
    assert built.engine == "swing"
    assert result.on == ON


# ----------------------------------------------------------------------- CLI


def _home(tmp_path, name: str, provider: FakeProvider):
    from trd.db.connection import connect

    home = tmp_path / name
    home.mkdir()
    conn = connect(home / "trd.duckdb")
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    service.scan()
    trade = service.position_rows()[0].position
    at = datetime.combine(ON, datetime.min.time()).replace(hour=16)
    service.positions.close(trade.id, at, trade.stop_price, "stopped", "stop")
    OutcomeService(conn).backfill()
    conn.close()
    return home


def test_cli_emits_a_pack_and_a_review_as_json(cli_env: FakeProvider, tmp_path) -> None:
    import json

    home = _home(tmp_path, "swing", cli_env)
    args = ["--engines", f"swing={home}", "--date", ON.isoformat(), "--json"]

    result = runner.invoke(app, ["engine", "review-pack", *args])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["engines"][0]["trades"][0]["rule"] == "stop"
    assert payload["engines"][0]["config"]["timeframe"] == "1d"

    result = runner.invoke(app, ["engine", "review", *args])
    assert result.exit_code == 0, result.output
    review_json = json.loads(result.output)
    assert review_json["quiet"] is True  # one trade proves nothing
    assert review_json["engines"] == ["swing"]


def test_cli_stores_the_review_dated(cli_env: FakeProvider, tmp_path) -> None:
    """So a claim made on Tuesday can be scored on Friday."""
    from trd.db.connection import connect
    from trd.repos.review_snapshot import ReviewSnapshotRepo

    home = _home(tmp_path, "swing", cli_env)
    result = runner.invoke(
        app,
        [
            "engine",
            "review",
            "--engines",
            f"swing={home}",
            "--date",
            ON.isoformat(),
            "--snapshot",
        ],
    )
    assert result.exit_code == 0, result.output

    conn = connect(home / "trd.duckdb")
    try:
        rows = ReviewSnapshotRepo(conn).list_recent()
        assert len(rows) == 1
        assert rows[0].snapshot_date == ON
        assert rows[0].engine == "swing"
        assert rows[0].quiet is True
        payload = ReviewSnapshotRepo(conn).payload(ON)
        assert payload is not None
        assert payload["pack"]["engine"] == "swing"
    finally:
        conn.close()


# -------------------------------------------------------------- the message


def test_the_review_message_puts_the_arithmetic_before_the_model() -> None:
    """One Telegram message a night: what a detector computed, then what a
    model concluded, visibly separate — different kinds of claim."""
    from types import SimpleNamespace

    from trd.notify.messages import review_message

    packs = [pack(window("momentum", 40, mfe="2.0", exit_r="0.2"))]
    result = review(packs, ON)
    assert result.findings, "the fixture is meant to plant a low-capture finding"

    judged = SimpleNamespace(
        review=SimpleNamespace(
            summary="Today said little; the window says the exits are slow.",
            nothing_conclusive=False,
            findings=[
                SimpleNamespace(
                    headline="momentum keeps a tenth of what it is offered",
                    engine="swing",
                    scope="strategy",
                    subject="momentum",
                    rests_on=["capture 0.10 over 40 trades"],
                    trades=40,
                    hypothesis=False,
                    test="trd engine backtest --years 5",
                )
            ],
            watch_next=["whether capture recovers with a trail"],
        ),
        usage=SimpleNamespace(model="anthropic:claude-opus-5", cost_usd=Decimal("0.2036")),
    )
    text = review_message(result, packs, judged)
    assert text.startswith("🔍 trd review — Thu Sep 3")
    assert "ARITHMETIC — 2 findings" in text
    assert "FINDING swing/momentum" in text and "n=40 over 30d" in text
    assert "THE MODEL'S READ (anthropic:claude-opus-5 · $0.20)" in text
    assert text.index("ARITHMETIC") < text.index("THE MODEL'S READ")
    assert "rests on: capture 0.10 over 40 trades" in text
    assert "WATCH NEXT" in text
    assert len(text) <= 4096


def test_the_review_message_says_nothing_conclusive_out_loud() -> None:
    from trd.notify.messages import review_message

    packs = [pack(window("momentum", 3))]
    text = review_message(review(packs, ON), packs, None)
    assert "ARITHMETIC — nothing conclusive today" in text
    assert "MODEL" not in text  # no model ran, no section pretending one did


def test_the_review_message_stays_under_telegrams_limit() -> None:
    """Telegram rejects a message over 4096 characters whole, not the tail."""
    from types import SimpleNamespace

    from trd.notify.messages import REVIEW_MESSAGE_BUDGET, review_message

    packs = [pack(window("momentum", 40, mfe="2.0", exit_r="0.2"))]
    long = SimpleNamespace(
        headline="x" * 300,
        engine="swing",
        scope="strategy",
        subject="momentum",
        rests_on=["y" * 300] * 5,
        trades=40,
        hypothesis=False,
        test="z" * 300,
    )
    judged = SimpleNamespace(
        review=SimpleNamespace(
            summary="s" * 2000,
            nothing_conclusive=False,
            findings=[long] * 12,
            watch_next=["w" * 500] * 8,
        ),
        usage=SimpleNamespace(model="m", cost_usd=None),
    )
    text = review_message(review(packs, ON), packs, judged)
    assert len(text) <= REVIEW_MESSAGE_BUDGET
    assert text.endswith("the rest is in trd engine review")


# ------------------------------------------------------------------- notify


def test_cli_notify_sends_nothing_on_a_date_with_no_session(
    cli_env: FakeProvider, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A holiday must never post "nothing conclusive" — it trains the reader to
    ignore the message on the day it says something."""
    import trd.cli.app as cli

    home = _home(tmp_path, "swing", cli_env)
    sent: list[str] = []

    class Spy:
        def send(self, text: str) -> None:
            sent.append(text)

    monkeypatch.setattr(cli, "notify_from_env", lambda: Spy())
    holiday = (ON + timedelta(days=200)).isoformat()  # no bar anywhere near it
    result = runner.invoke(
        app, ["engine", "review", "--engines", f"swing={home}", "--date", holiday, "--notify"]
    )
    assert result.exit_code == 0, result.output
    assert "nothing sent" in result.output
    assert sent == []


def test_cli_notify_sends_the_review_when_the_session_traded(
    cli_env: FakeProvider, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import trd.cli.app as cli

    home = _home(tmp_path, "swing", cli_env)
    sent: list[str] = []

    class Spy:
        def send(self, text: str) -> None:
            sent.append(text)

    monkeypatch.setattr(cli, "notify_from_env", lambda: Spy())
    # The session that "traded" is the last bar the fixture stored, not ON: the
    # market_open rule is "some engine has a bar for this date", same as the report.
    traded = make_bars(uptrend())[-1].date.isoformat()
    result = runner.invoke(
        app,
        ["engine", "review", "--engines", f"swing={home}", "--date", traded, "--notify"],
    )
    assert result.exit_code == 0, result.output
    assert len(sent) == 1
    assert sent[0].startswith("🔍 trd review")
    assert "ARITHMETIC" in sent[0]
