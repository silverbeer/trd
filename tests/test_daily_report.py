"""The post-market report: up or down, what worked, what did not.

The failure this guards against is not a wrong sum — it is a report that reads as
confident when it is not. So the tests here care as much about what the message
*refuses* to say (a net without its halves, a strategy crowned on two trades, a
number drawn from a stale mark, a "flat day" on a market holiday) as about the
arithmetic behind it.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.cli.app import app
from trd.models import EnginePosition, PositionStatus
from trd.notify.messages import MAX_LINES, daily_report_message
from trd.services import EngineService
from trd.services.daily_report import (
    MIN_WINDOW_TRADES,
    DailyReport,
    EngineDay,
    ExitEvent,
    _best_and_worst,
    engine_day,
    failed_engine,
)

runner = CliRunner()

TODAY = date(2026, 9, 3)


# --------------------------------------------------------------- hand-built rows


def closed(
    strategy: str,
    r: str,
    on: date = TODAY,
    entry: str = "100",
    stop: str = "90",
) -> EnginePosition:
    """A closed trade that booked exactly `r` times its risk.

    Built through `book_exit` rather than by setting fields, so the R here is the
    same R the scorecard and the live engine compute.
    """
    entry_price, stop_price = Decimal(entry), Decimal(stop)
    risk = entry_price - stop_price
    position = EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy=strategy,
        opened_at=datetime.combine(on, datetime.min.time()) - timedelta(days=1),
        entry_price=entry_price,
        quantity=Decimal("10"),
        stop_price=stop_price,
        target_price=entry_price * 2,
        atr_at_entry=Decimal("5"),
        trail_high=entry_price,
        status=PositionStatus.CLOSED,
        closed_at=datetime.combine(on, datetime.min.time()).replace(hour=16),
    )
    position.book_exit(position.quantity, entry_price + risk * Decimal(r))
    return position


def event(
    symbol: str,
    pnl: str,
    rule: str | None = "stop",
    r: str = "-1.00",
    entry: str = "100",
    exit_: str = "90",
    bars_held: int = 3,
    minutes: int = 0,
) -> ExitEvent:
    opened = datetime.combine(TODAY, datetime.min.time()).replace(hour=9, minute=31)
    return ExitEvent(
        symbol=symbol,
        strategy="momentum",
        rule=rule,
        pnl=Decimal(pnl),
        r_multiple=Decimal(r),
        entry_price=Decimal(entry),
        exit_price=Decimal(exit_),
        quantity=Decimal("10"),
        opened_at=opened,
        closed_at=opened + timedelta(minutes=minutes) if minutes else opened + timedelta(days=1),
        bars_held=bars_held,
    )


def day(
    name: str = "swing",
    *,
    timeframe: str = "1d",
    last_session: date | None = TODAY,
    realized_today: Decimal = Decimal(0),
    exits_today: list[ExitEvent] | None = None,
    realized_all: Decimal = Decimal("100"),
    unrealized: Decimal = Decimal("-20"),
    marks_are_stale: bool = False,
    marked_at: date | None = None,
) -> EngineDay:
    """One engine's day, assembled by hand so the message tests read as English."""
    return EngineDay(
        engine=name,
        account="engine-sim",
        timeframe=timeframe,
        last_session=last_session,
        realized_today=realized_today,
        exits_today=exits_today or [],
        realized_all=realized_all,
        unrealized=unrealized,
        open_positions=2,
        risk_at_stop=Decimal("50"),
        marks_are_stale=marks_are_stale,
        marked_at=marked_at,
    )


# ------------------------------------------------------- best and worst strategy


def test_a_strategy_is_not_named_until_it_has_said_something() -> None:
    """Two trades is an expectancy, not evidence. A report that crowns a coin
    flip every night costs more attention than it returns."""
    thin = [closed("momentum", "2") for _ in range(MIN_WINDOW_TRADES - 1)]
    assert _best_and_worst(thin) == (None, None)


def test_best_and_worst_are_ranked_on_expectancy_in_r() -> None:
    """R, not dollars: a $200 day trade and a $2,000 swing have to be comparable
    or the ranking just finds whichever engine sizes bigger."""
    trades = [
        *[closed("momentum", "-1") for _ in range(3)],
        *[closed("pullback", "2") for _ in range(3)],
    ]
    best, worst = _best_and_worst(trades)
    assert best is not None and worst is not None
    assert best.strategy == "pullback"
    assert best.expectancy_r == Decimal("2")
    assert worst.strategy == "momentum"


def test_one_strategy_is_never_both_the_best_and_the_worst() -> None:
    """Naming the same rule twice reads as a bug in the report, not as a fact
    about the engine."""
    best, worst = _best_and_worst([closed("momentum", "1") for _ in range(3)])
    assert best is not None and best.strategy == "momentum"
    assert worst is None


# --------------------------------------------------------------- the day, per engine


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    """An engine holding one open AAA position, the way a scan leaves it."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    assert len(service.scan().opened) == 1
    return service


def test_the_exit_rule_is_stored_as_a_key_not_only_as_prose(engine: EngineService) -> None:
    """`exit_reason` carries the numbers that fired the rule, so every row is
    unique and nothing can group by it. The key is what makes "three stops and a
    bell" a countable statement."""
    position = engine.position_rows()[0].position
    engine.positions.close(
        position.id,
        datetime.combine(TODAY, datetime.min.time()).replace(hour=16),
        position.stop_price,
        "hit the stop at 90.00 — thesis broke, lost 1R (10.00/share)",
        "stop",
    )
    stored = engine.positions.list_closed(engine.account().id)[0][0]
    assert stored.exit_rule == "stop"
    assert "thesis broke" in (stored.exit_reason or "")


def test_only_trades_closed_on_the_reported_session_count_as_today(
    engine: EngineService,
) -> None:
    """The window behind "what is working" is 30 days; "today" is one session.
    Mixing them is how a flat day reports last week's win."""
    position = engine.position_rows()[0].position
    engine.positions.close(
        position.id,
        datetime.combine(TODAY - timedelta(days=2), datetime.min.time()).replace(hour=16),
        position.entry_price + Decimal("5"),
        "target reached",
        "target",
    )

    report = engine_day(engine, "swing", TODAY)
    assert report.exits_today == []
    assert report.realized_today == Decimal(0)
    # Still in the trailing window, and still in the all-time realized total.
    assert report.window_trades == 1
    assert report.realized_all > 0


def test_todays_exits_carry_their_rule_and_sum_to_realized_today(
    engine: EngineService,
) -> None:
    position = engine.position_rows()[0].position
    at = datetime.combine(TODAY, datetime.min.time()).replace(hour=16)
    engine.positions.close(position.id, at, position.stop_price, "hit the stop", "stop")

    report = engine_day(engine, "swing", TODAY)
    assert [e.rule for e in report.exits_today] == ["stop"]
    assert report.realized_today == sum(e.pnl for e in report.exits_today)
    assert report.realized_today < 0
    assert report.losses_today and not report.wins_today


def test_an_unreadable_engine_never_costs_the_other_one_its_numbers() -> None:
    """One unmounted home must not blank the report. A daily message that
    disappears on a bad day is the failure this whole ticket is about."""
    report = DailyReport(
        on=TODAY, engines=[day("swing"), failed_engine("day", "no database at /engines/day")]
    )
    assert report.realized_all == Decimal("100")
    assert "day" in daily_report_message(report)
    assert "no database" in daily_report_message(report)


# --------------------------------------------------------------- combined totals


def test_combined_is_the_sum_of_two_reads_not_one_query() -> None:
    """One database is one engine, so "both" is arithmetic over separate reads."""
    report = DailyReport(
        on=TODAY,
        engines=[
            day("swing", realized_today=Decimal("10"), exits_today=[event("AAA", "10", r="1.0")]),
            day("day", realized_today=Decimal("-4"), exits_today=[event("BBB", "-4")]),
        ],
    )
    assert report.realized_today == Decimal("6")
    assert report.exits_today == 2
    assert report.net == report.realized_all + report.unrealized
    assert report.risk_at_stop == Decimal("100")


def test_the_market_is_open_only_when_an_engine_has_a_bar_for_the_date() -> None:
    """No trading calendar: a holiday is a date no engine has a session for. A
    report that posted "flat, nothing happened" every Thanksgiving would train
    its reader to ignore it."""
    holiday = DailyReport(on=TODAY, engines=[day(last_session=TODAY - timedelta(days=1))])
    assert holiday.market_open is False
    assert "may not have opened" in daily_report_message(holiday)
    assert DailyReport(on=TODAY, engines=[day()]).market_open is True


# ------------------------------------------------------------------- the message


def test_net_is_never_shown_without_realized_and_unrealized() -> None:
    """An engine up on net only because of open positions, while its completed
    trades lost money, is a different engine from one up on both."""
    text = daily_report_message(DailyReport(on=TODAY, engines=[day()]))
    net_line = next(line for line in text.splitlines() if "NET" in line)
    assert "realized" in net_line and "unrealized" in net_line


def test_stale_marks_are_stated_above_the_numbers_they_would_break() -> None:
    """A mark drawn from yesterday's close makes unrealized, risk and NET wrong
    in a way that reads exactly like a result."""
    text = daily_report_message(
        DailyReport(
            on=TODAY,
            engines=[day(marks_are_stale=True, marked_at=TODAY - timedelta(days=1))],
        )
    )
    lines = text.splitlines()
    assert "stale" in lines[1]
    assert lines.index("TODAY") > 1  # the warning comes first, not as a footnote


def test_todays_losses_are_grouped_by_exit_rule() -> None:
    """Five stops is a broken thesis; five bells is a day engine that never got
    paid. One total cannot tell them apart."""
    losses = [
        event("AAA", "-100", "stop"),
        event("BBB", "-50", "stop"),
        event("CCC", "-10", "session_close", r="-0.10"),
    ]
    text = daily_report_message(
        DailyReport(on=TODAY, engines=[day(exits_today=losses, realized_today=Decimal("-160"))])
    )
    stop_line = next(line for line in text.splitlines() if "Stop Loss" in line)
    assert "x2" in stop_line and "-150.00" in stop_line
    assert any("Session Close" in line for line in text.splitlines())


def test_a_trade_closed_before_the_rule_key_existed_reads_as_closed() -> None:
    """NULL `exit_rule` is every trade closed before migration 021. A guessed key
    would be indistinguishable from a recorded one, so it stays unnamed."""
    text = daily_report_message(
        DailyReport(on=TODAY, engines=[day(exits_today=[event("AAA", "-5", rule=None)])])
    )
    assert "closed x1" in text


def test_what_went_badly_is_the_section_that_gives_way_on_a_crowded_screen() -> None:
    """The ticket's own trim rule: cut section 3 before section 1. Scrolling past
    the detail to find out whether you are up is how a daily message stops being
    read."""
    crowded = DailyReport(
        on=TODAY,
        engines=[
            day(f"engine-{i}", exits_today=[event(f"S{i}", "-5")], realized_today=Decimal("-5"))
            for i in range(8)
        ],
    )
    text = daily_report_message(crowded)
    assert "NOT WORKING" not in text
    assert "TODAY" in text and "OPEN BOOK" in text  # sections 1 and 4 survive

    # Sections 1, 2 and 4 are bounded by the engine count and are never cut, so
    # the ceiling only governs what is optional: the two-engine case — the one
    # that actually ships — keeps its losses and still fits.
    two = DailyReport(on=TODAY, engines=crowded.engines[:2])
    assert "NOT WORKING" in daily_report_message(two)
    assert len(daily_report_message(two).splitlines()) <= MAX_LINES


def test_one_engine_says_nothing_about_both() -> None:
    """The combined line is noise when there is only one engine to combine."""
    text = daily_report_message(DailyReport(on=TODAY, engines=[day()]))
    assert not any(line.startswith("both") for line in text.splitlines())


# ----------------------------------------------------------------------- the CLI


def _home(tmp_path, name: str, provider: FakeProvider, closes: list[float] | None = None):
    """A seeded engine home on disk, the way a deployed engine's TRD_HOME looks."""
    home = tmp_path / name
    home.mkdir()
    conn = None
    try:
        from trd.db.connection import connect

        conn = connect(home / "trd.duckdb")
        bars = make_bars(closes or uptrend())
        seed(conn, "AAA", bars)
        provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
        service = EngineService(conn, provider)
        service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
        service.scan()
    finally:
        if conn is not None:
            conn.close()
    return home


def test_cli_reports_both_engines_as_json(cli_env: FakeProvider, tmp_path) -> None:
    import json

    swing = _home(tmp_path, "swing", cli_env)
    intraday = _home(tmp_path, "day", cli_env)
    result = runner.invoke(
        app,
        ["engine", "daily-report", "--engines", f"swing={swing},day={intraday}", "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [e["engine"] for e in payload["engines"]] == ["swing", "day"]
    assert Decimal(payload["net"]) == sum(Decimal(e["net"]) for e in payload["engines"])


def test_cli_reports_a_missing_home_without_failing_the_run(
    cli_env: FakeProvider, tmp_path
) -> None:
    """Exit 0 on purpose: this runs from a CronJob, and a non-zero exit would
    turn a named problem into a pod that just failed."""
    swing = _home(tmp_path, "swing", cli_env)
    result = runner.invoke(
        app,
        ["engine", "daily-report", "--engines", f"swing={swing},day={tmp_path}/gone"],
    )
    assert result.exit_code == 0, result.output
    assert "no database" in result.output


def test_cli_falls_back_to_this_home_when_no_engines_are_named(
    cli_env: FakeProvider, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runnable by hand against one engine, which is what makes it testable
    offline and debuggable on a laptop."""
    home = _home(tmp_path, "solo", cli_env)
    monkeypatch.setenv("TRD_HOME", str(home))
    monkeypatch.setenv("TRD_ENGINE_LABEL", "solo")
    monkeypatch.delenv("TRD_BOT_ENGINES", raising=False)
    result = runner.invoke(app, ["engine", "daily-report"])
    assert result.exit_code == 0, result.output
    assert "solo" in result.output


# ------------------------------------------------------------- today's trades


def test_the_trade_list_names_the_legs_the_hold_and_the_rule() -> None:
    """The count in TODAY provokes exactly one question — which trades? Until
    this line existed, answering it meant filtering `engine positions --all
    --json` by date, which is a missing command wearing a query's clothes."""
    text = daily_report_message(
        DailyReport(
            on=TODAY,
            engines=[day(exits_today=[event("COIN", "6.46", "time", r="0.59", exit_="183.26")])],
        )
    )
    line = next(line for line in text.splitlines() if "COIN" in line)
    assert "+6.46" in line and "(+0.59R)" in line
    assert "100.00 → 183.26" in line
    assert "3 sessions" in line
    assert "Time Exit" in line  # the rule's own name, not its key


def test_hold_is_sessions_on_a_swing_engine_and_elapsed_time_on_an_intraday_one() -> None:
    """The same trade is "3 bars" and "19 minutes"; only one of those tells an
    intraday reader anything, and only the other means something on a daily."""
    trade = event("HOOD", "0.41", "target", r="2.24", minutes=49)
    swing = daily_report_message(DailyReport(on=TODAY, engines=[day(exits_today=[trade])]))
    assert "3 sessions" in swing

    intraday = daily_report_message(
        DailyReport(on=TODAY, engines=[day(timeframe="5m", exits_today=[trade])])
    )
    assert "49m" in intraday
    assert "sessions" not in intraday.split("TODAY'S TRADES")[1]


def test_the_win_of_the_day_is_the_first_line_and_the_worst_is_the_last(
    engine: EngineService,
) -> None:
    """Ranked in R, not dollars: a $10 day trade at +2R beat a $100 swing at
    +0.5R, and a list sorted by cash would say the opposite."""
    position = engine.position_rows()[0].position
    at = datetime.combine(TODAY, datetime.min.time()).replace(hour=16)
    engine.positions.close(position.id, at, position.stop_price, "hit the stop", "stop")

    report = engine_day(engine, "swing", TODAY)
    ranks = [e.r_multiple for e in report.exits_today if e.r_multiple is not None]
    assert ranks == sorted(ranks, reverse=True)


def test_a_truncated_trade_list_keeps_both_ends() -> None:
    """A best-first list cut to a prefix hides every loser behind '+N more', and
    what went badly is half of what the report is for."""
    trades = [
        event(f"W{i}", str(10 - i), "target", r=f"{2 - i * 0.1:.2f}", exit_="110") for i in range(6)
    ] + [event("LOSER", "-9", "stop", r="-1.40")]
    text = daily_report_message(DailyReport(on=TODAY, engines=[day(exits_today=trades)]))
    assert "W0" in text  # the win of the day
    assert "LOSER" in text  # and the worst of it
    assert "more" in text


def test_the_trade_list_gives_way_before_the_losses_summary() -> None:
    """Which rule cost you money outranks which ticker did. Both outrank
    nothing, and both are cut before the four sections that are the report."""
    crowded = DailyReport(
        on=TODAY,
        engines=[
            day(f"engine-{i}", exits_today=[event(f"S{i}", "-5")], realized_today=Decimal("-5"))
            for i in range(6)
        ],
    )
    text = daily_report_message(crowded)
    assert "TODAY'S TRADES" not in text
    assert "NOT WORKING" in text


def test_json_carries_the_legs_so_nothing_has_to_re_derive_them(
    cli_env: FakeProvider, tmp_path
) -> None:
    import json

    home = _home(tmp_path, "swing", cli_env)
    conn = None
    try:
        from trd.db.connection import connect

        conn = connect(home / "trd.duckdb")
        service = EngineService(conn, cli_env)
        position = service.position_rows()[0].position
        service.positions.close(
            position.id, datetime.now(), position.stop_price, "hit the stop", "stop"
        )
    finally:
        if conn is not None:
            conn.close()

    result = runner.invoke(app, ["engine", "daily-report", "--engines", f"swing={home}", "--json"])
    assert result.exit_code == 0, result.output
    exit_row = json.loads(result.output)["engines"][0]["exits_today"][0]
    assert Decimal(exit_row["entry_price"]) > 0
    assert Decimal(exit_row["exit_price"]) > 0
    assert exit_row["opened_at"] and exit_row["closed_at"]
    assert exit_row["rule"] == "stop"
