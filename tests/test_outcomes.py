"""Trade outcomes — MAE, MFE, capture, follow-through, and the passed signals.

Every fixture here is hand-built so each expected number is known by
construction rather than by snapshotting whatever the live engine happened to
produce. A study of whether the rules are any good cannot itself be checked
against the rules' own output.

Entry is 100 with a 10-wide stop throughout, so 1R = 10 and every assertion
below can be read as "the bar touched 90, which is -1R".
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.cli.app import app
from trd.engine.bars import BarSource
from trd.models import DailyBar, EnginePosition, PositionStatus
from trd.services import EngineService
from trd.services.outcomes import (
    OutcomeService,
    _pooled_capture,
    excursion,
    follow_through_bars,
    resolution,
    signal_outcome,
    trade_outcome,
    window,
)

runner = CliRunner()

DAILY = BarSource.stamper("1d")
ENTRY = Decimal("100")
RISK = Decimal("10")  # 1R, the stop distance


def bar(day: int, low: str, high: str, close: str | None = None) -> DailyBar:
    """One session with the extremes that matter and a close that need not."""
    return DailyBar(
        date=date(2026, 6, day),
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close or high),
        volume=1_000_000,
    )


def position(closed_at: datetime = datetime(2026, 6, 4, 16, 0)) -> EnginePosition:
    """A closed trade entered at 100 with its stop 10 below — 1R = 10, so every
    price in a fixture reads directly as an R-multiple."""
    return EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy="momentum",
        opened_at=datetime(2026, 6, 1, 15, 0),
        entry_price=ENTRY,
        quantity=Decimal("10"),
        stop_price=ENTRY - RISK,
        target_price=ENTRY + RISK * 2,
        atr_at_entry=Decimal("5"),
        trail_high=ENTRY,
        status=PositionStatus.CLOSED,
        closed_at=closed_at,
    )


# ------------------------------------------------------------------ excursions


def test_excursion_measures_the_extremes_not_the_closes() -> None:
    """A trade lives through the whole of every bar it is in. Measuring on
    closes would report a stop-out as a quiet -0.4R day."""
    bars = [bar(2, low="94", high="104"), bar(3, low="88", high="118", close="99")]
    ex = excursion(bars, ENTRY, RISK, DAILY)
    assert ex.mae_r == Decimal("-1.2")  # 88 is 12 below entry, 1.2R
    assert ex.mfe_r == Decimal("1.8")  # 118 is 18 above entry
    assert ex.mae_bar == 2 and ex.mfe_bar == 2
    assert ex.bars_seen == 2


def test_the_bar_index_is_the_half_of_mfe_that_says_what_to_do() -> None:
    """The same +2R peak on bar 1 of a ten-bar hold and on the last bar are
    opposite findings: one is an exit rule that is too slow, one is not."""
    early = excursion(
        [bar(2, "99", "120"), *[bar(d, "99", "101") for d in range(3, 12)]], ENTRY, RISK, DAILY
    )
    assert early.mfe_r == Decimal("2") and early.mfe_bar == 1


def test_a_trade_that_never_went_underwater_has_no_adverse_excursion() -> None:
    """Zero, not a positive number: MAE is read as a negative, and a +0.4R "worst
    case" would invert the sign the whole column is scanned with."""
    ex = excursion([bar(2, "104", "110")], ENTRY, RISK, DAILY)
    assert ex.mae_r == Decimal(0)
    assert ex.mfe_r == Decimal("1")


def test_the_entry_bar_is_not_part_of_the_trade() -> None:
    """The engine fills at the entry bar's close, so that bar's low already
    happened. Counting it would credit the trade with heat it never took."""
    bars = [bar(1, low="50", high="200"), bar(2, low="95", high="105")]
    lived, forward = window(bars, DAILY, datetime(2026, 6, 1, 15, 0), datetime(2026, 6, 2, 16, 0))
    # Stamped rather than read off the model: `window` returns Bars, and the
    # stamp is the one thing every timeframe's bar agrees on.
    assert [DAILY.stamp(b).day for b in lived] == [2]
    assert forward == []
    assert excursion(lived, ENTRY, RISK, DAILY).mae_r == Decimal("-0.5")


# ---------------------------------------------------------------- resolution


def test_resolution_takes_whichever_level_came_first() -> None:
    # +2R is 120 and the 1R stop is 90. Bar 2 reaches 125 with its low at 96, so
    # the target is hit while the stop never is; bar 3 collapsing afterwards is
    # what the trade would have already exited before.
    bars = [bar(2, "96", "125"), bar(3, "80", "126")]
    assert resolution(bars, ENTRY, RISK) == "target"
    assert resolution([bar(2, "88", "104"), bar(3, "99", "125")], ENTRY, RISK) == "stop"
    assert resolution([bar(2, "95", "105")], ENTRY, RISK) == "neither"


def test_a_bar_that_spans_both_levels_is_scored_as_the_stop() -> None:
    """Bar data cannot say which came first inside the bar, and an outcome study
    that resolves its own ambiguity in its favour is worthless."""
    assert resolution([bar(2, "85", "125")], ENTRY, RISK) == "stop"


# -------------------------------------------------------------- trade outcome


def series() -> list[DailyBar]:
    return [
        bar(1, "99", "101"),  # the entry bar, excluded
        bar(2, "95", "108"),
        bar(3, "97", "116"),  # MFE +1.6R
        bar(4, "104", "112", close="110"),  # exit day
        bar(5, "108", "121", close="120"),  # after the exit
        bar(6, "115", "130", close="130"),
    ]


def test_capture_is_what_was_kept_of_what_was_offered() -> None:
    trade = position()
    trade.book_exit(trade.quantity, Decimal("110"))  # +1R booked
    outcome = trade_outcome(trade, series(), DAILY, horizon=2)
    assert outcome is not None
    assert outcome.mfe_r == Decimal("1.6")
    assert outcome.exit_r == Decimal("1")
    assert outcome.capture is not None
    assert round(float(outcome.capture), 4) == 0.625  # 1.0 / 1.6
    assert outcome.gave_back_r == Decimal("0.6")


def test_follow_through_says_whether_we_left_too_early() -> None:
    """The direct answer to the question. Price closed at 130 two bars after we
    sold at 110 — three more R the exit rule left on the table."""
    trade = position()
    trade.book_exit(trade.quantity, Decimal("110"))
    outcome = trade_outcome(trade, series(), DAILY, horizon=2)
    assert outcome is not None
    assert outcome.follow_through_r == Decimal("2")  # (130 - 110) / 10
    assert outcome.follow_through_seen == 2
    assert outcome.exited_early is True


def test_a_trade_with_no_future_yet_says_so_rather_than_reporting_zero() -> None:
    """A trade that closed on the last stored bar has no follow-through. Zero
    bars must never read as "it went nowhere"."""
    trade = position(closed_at=datetime(2026, 6, 6, 16, 0))
    trade.book_exit(trade.quantity, Decimal("130"))
    outcome = trade_outcome(trade, series(), DAILY, horizon=5)
    assert outcome is not None
    assert outcome.follow_through_seen == 0
    assert outcome.follow_through_r is None
    assert outcome.exited_early is None


def test_capture_is_none_when_nothing_was_ever_offered() -> None:
    """You cannot give back what you never had, and a 0% would read as an exit
    that failed rather than an entry that never worked."""
    down = [bar(1, "99", "101"), bar(2, "88", "99", close="90")]
    trade = position(closed_at=datetime(2026, 6, 2, 16, 0))
    trade.book_exit(trade.quantity, Decimal("90"))
    outcome = trade_outcome(trade, down, DAILY, horizon=2)
    assert outcome is not None
    assert outcome.mfe_r == Decimal(0)
    assert outcome.capture is None
    assert outcome.exit_r == Decimal("-1")


def test_capture_is_pooled_because_averaging_ratios_invents_findings() -> None:
    """Observed on the live book: the mean of per-trade capture read -507%, an
    artefact of dividing by denominators that differ by orders of magnitude. One
    trade that peaked at +0.02R and stopped at -1R scores -50 on its own.
    """
    tiny = position()
    tiny.book_exit(tiny.quantity, Decimal("90"))
    outcomes = []
    for mfe, exit_price in ((Decimal("0.02"), Decimal("90")), (Decimal("2"), Decimal("120"))):
        trade = position()
        trade.book_exit(trade.quantity, exit_price)
        bars = [
            bar(1, "99", "101"),
            bar(2, "90", str(100 + float(mfe) * 10), close=str(exit_price)),
        ]
        measured = trade_outcome(trade, bars, DAILY, horizon=1)
        assert measured is not None
        outcomes.append(measured)

    per_trade = [o.capture for o in outcomes if o.capture is not None]
    assert min(per_trade) < Decimal("-10")  # the artefact, still visible per trade
    pooled = _pooled_capture(outcomes)
    assert pooled is not None
    assert Decimal("-1") < pooled < Decimal("2")  # the honest aggregate


# ------------------------------------------------------------- signal outcome


def test_a_signal_with_no_stop_distance_is_left_unmeasured() -> None:
    """Without the stop the engine would have used there is no R, and a
    counterfactual in dollars is not comparable with anything else here."""
    from trd.models import EngineSignal

    signal = EngineSignal(
        id=1,
        instrument_id=1,
        strategy="momentum",
        bar_ts=datetime(2026, 6, 1),
        fired_at=datetime(2026, 6, 1, 15, 0),
        price=ENTRY,
        score=1.0,
        reason="test",
    )
    assert signal_outcome(signal, series(), DAILY, None, 5, 0, 5) is None


def test_a_passed_signal_that_fired_on_a_full_book_is_marked_as_such() -> None:
    """Most passed signals could not have been taken. Counting their returns as
    money left on the table would be fiction, so the flag is what makes the
    'takeable' population mean anything."""
    from trd.models import EngineSignal

    signal = EngineSignal(
        id=1,
        instrument_id=1,
        strategy="momentum",
        bar_ts=datetime(2026, 6, 1),
        fired_at=datetime(2026, 6, 1, 15, 0),
        price=ENTRY,
        score=1.0,
        reason="test",
        acted=False,
    )
    full = signal_outcome(signal, series(), DAILY, RISK, 5, open_positions=5, max_positions=5)
    room = signal_outcome(signal, series(), DAILY, RISK, 5, open_positions=2, max_positions=5)
    assert full is not None and room is not None
    assert full.capacity_blocked is True
    assert room.capacity_blocked is False
    # A taken signal is never "blocked", whatever the book looked like.
    taken = signal.model_copy(update={"acted": True})
    measured = signal_outcome(taken, series(), DAILY, RISK, 5, 5, 5)
    assert measured is not None and measured.capacity_blocked is False


def test_the_horizon_is_the_engine_s_own_timeframe() -> None:
    """Five sessions on a swing engine; an hour on an intraday one, because five
    sessions after a 20-minute trade is a different market."""
    assert follow_through_bars("1d") == 5
    assert follow_through_bars("5m") == 12
    assert follow_through_bars("1h") == 1


# ------------------------------------------------------------------ the service


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    service = EngineService(conn, provider)
    service.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    assert len(service.scan().opened) == 1
    return service


def test_backfill_is_idempotent(engine: EngineService, conn: duckdb.DuckDBPyConnection) -> None:
    """It runs after every close and over all history. Running it twice must
    measure nothing the second time, or 'stored, not recomputed' is a lie."""
    trade = engine.position_rows()[0].position
    engine.positions.close(
        trade.id, trade.opened_at + timedelta(days=3), trade.stop_price, "stopped", "stop"
    )
    service = OutcomeService(conn)

    first = service.backfill()
    assert first.trades_measured == 1

    second = service.backfill()
    assert second.trades_measured == 0
    assert second.trades_skipped >= 1
    assert len(service.trades.list_all()) == 1  # not duplicated


def test_the_summary_states_its_own_caveats(engine: EngineService, conn) -> None:
    """Simulation fills and survivorship are properties of the measurement, not
    footnotes for the reader to remember."""
    service = OutcomeService(conn)
    service.backfill()
    summary = service.summary()
    assert any("Simulation fills" in c for c in summary.caveats)
    assert any("Survivorship" in c for c in summary.caveats)  # the open position
    assert summary.open_trades == 1


def test_cli_backfills_and_emits_json(cli_env: FakeProvider, tmp_path) -> None:
    import json

    from trd.db.connection import connect

    home = tmp_path / "engine"
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
        trade.id, trade.opened_at + timedelta(days=3), trade.entry_price * 2, "target", "target"
    )
    conn.close()

    import os

    os.environ["TRD_HOME"] = str(home)
    try:
        assert runner.invoke(app, ["engine", "outcomes", "--backfill"]).exit_code == 0
        result = runner.invoke(app, ["engine", "outcomes", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["measured_trades"] == 1
        assert payload["trades"]["count"] == 1
        assert any("Simulation fills" in c for c in payload["caveats"])
    finally:
        os.environ.pop("TRD_HOME", None)
