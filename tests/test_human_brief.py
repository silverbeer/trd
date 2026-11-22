"""The human-slot brief (SB-1754): market state, stretched names that turned,
and the measurement that says how far to trust the list.

Every series here is synthetic and built to put one signal on or off; nothing
reaches a provider.
"""

import json
import math
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.db.connection import connect
from trd.models import DailyBar, EarningsDate
from trd.notify.messages import human_brief_message
from trd.repos import InstrumentRepo
from trd.repos.earnings import EarningsRepo
from trd.services import EngineService
from trd.services.human import HumanService
from trd.services.human_brief import (
    LONG_SMA,
    MEASURED_NOTE,
    HumanBriefService,
    Series,
    breadth_thrust,
    capitulation_bar,
    fires,
    higher_low,
    higher_low_at,
    leadership_flip,
    market_state,
    reclaim_at,
    rsi_divergence,
    sessions_ahead,
    vix_rollover,
)

from .conftest import FakeProvider
from .test_engine import seed
from .test_engine_live import _filled_position, _live
from .test_engine_outbox import _no_telegram

__all__ = ["_no_telegram"]  # autouse: no inherited Telegram config

D = Decimal
runner = CliRunner()
END = date(2026, 3, 13)
NOW = datetime(2026, 3, 13, 10, 0)


def make(closes: list[float], end: date = END, **overrides: dict[int, float]) -> list[DailyBar]:
    """Daily bars ending on `end`. High/low straddle the close by 0.5% unless
    `opens` / `highs` / `lows` / `volumes` name a bar (negative index allowed)."""
    n = len(closes)

    def pick(kind: str, i: int, default: float) -> float:
        table = overrides.get(kind, {})
        return table.get(i, table.get(i - n, default))

    out: list[DailyBar] = []
    for i, close in enumerate(closes):
        out.append(
            DailyBar(
                date=end - timedelta(days=n - 1 - i),
                open=D(str(round(pick("opens", i, close), 4))),
                high=D(str(round(pick("highs", i, close * 1.005), 4))),
                low=D(str(round(pick("lows", i, close * 0.995), 4))),
                close=D(str(round(close, 4))),
                volume=int(pick("volumes", i, 1_000_000)),
            )
        )
    return out


# Stretched ~16% below its 200-session average, then a low that held and a
# close through the prior session's high.
TURNED = [100.0] * 205 + [98 - 2 * k for k in range(10)] + [81, 82, 83, 81.5, 83.5]
# Still falling: the low is the last bar.
FALLING = [100.0] * 205 + [100 - 1.4 * (k + 1) for k in range(16)]
# Sat on its 20-session average, then closed above it.
RECLAIMED = [100.0] * 200 + [96, 92, 88, 84, 80] + [80.0] * 40 + [82.0]
FLAT = [100.0 + math.sin(i / 3.0) for i in range(221)]


# ------------------------------------------------------------------- the rule


def test_a_stretched_name_that_held_a_higher_low_fires() -> None:
    s = Series(make(TURNED))
    i = len(s) - 1
    assert (s.stretch(i) or 0) < -0.10
    assert higher_low_at(s, i) is True
    assert not reclaim_at(s, i)
    assert fires(s, i) == "higher_low"


def test_a_stretched_name_still_falling_does_not_fire() -> None:
    s = Series(make(FALLING))
    i = len(s) - 1
    assert (s.stretch(i) or 0) < -0.10
    assert higher_low_at(s, i) is False  # the low is the last bar
    assert fires(s, i) is None


def test_a_reclaim_of_the_short_average_fires() -> None:
    s = Series(make(RECLAIMED))
    i = len(s) - 1
    assert reclaim_at(s, i)
    assert fires(s, i) == "reclaim"
    assert fires(s, i - 1) is None  # the day before it sat on the average


def test_a_turn_without_the_stretch_does_not_fire() -> None:
    # The same turn, 4% under the average instead of 16%.
    shallow = [100.0] * 205 + [99.5 - 0.5 * k for k in range(10)] + [95.2, 95.5, 95.8, 95.4, 96.5]
    s = Series(make(shallow))
    i = len(s) - 1
    assert higher_low_at(s, i) is True
    assert fires(s, i) is None


def test_too_little_history_is_unknown_not_off() -> None:
    s = Series(make([100.0] * 10))
    assert higher_low_at(s, len(s) - 1) is None
    assert s.stretch(len(s) - 1) is None
    assert fires(s, len(s) - 1) is None


def test_a_price_break_sets_the_name_aside() -> None:
    """A split stored unadjusted looks exactly like 'stretched'."""
    broken = [100.0] * 205 + [40.0] * 11 + [41, 42, 43, 41.5, 43.5]
    s = Series(make(broken))
    i = len(s) - 1
    assert s.breaks == [205]
    assert s.break_in_window(i) == 205
    assert higher_low_at(s, i) is True
    assert fires(s, i) is None
    # Once the long average no longer spans the break it is screened again.
    assert Series(make([100.0] * 5 + [40.0] * (LONG_SMA + 1))).break_in_window(LONG_SMA + 5) is None


def test_sessions_ahead_skips_weekends_and_market_holidays() -> None:
    # Wed 25 Nov 2026: Thursday is Thanksgiving, Friday trades, then Monday.
    assert sessions_ahead(date(2026, 11, 25), 1) == date(2026, 11, 27)
    assert sessions_ahead(date(2026, 11, 25), 2) == date(2026, 11, 30)
    assert sessions_ahead(date(2026, 11, 20), 10) == date(2026, 12, 7)


# ---------------------------------------------------------------- market state


def _decline(n: int = 40) -> list[float]:
    return [120.0 - 0.5 * i for i in range(n)]


def test_vix_rollover_is_on_when_fear_peaked_before_the_low() -> None:
    spy = Series(make(_decline()))
    fear = [20.0 + 0.3 * i for i in range(40)]
    fear[-6] = 45.0  # the peak, five sessions before the index low
    signal = vix_rollover(spy, Series(make(fear)))
    assert signal.on is True
    assert "VIX peak 45.0" in signal.detail


def test_vix_rollover_is_off_when_fear_peaks_with_the_low() -> None:
    spy = Series(make(_decline()))
    signal = vix_rollover(spy, Series(make([20.0 + 0.5 * i for i in range(40)])))
    assert signal.on is False


def test_vix_rollover_is_off_without_a_fresh_low() -> None:
    recovering = [*_decline(30), *[106.0 + i for i in range(10)]]
    signal = vix_rollover(Series(make(recovering)), Series(make([30.0] * 40)))
    assert signal.on is False
    assert "no fresh index low" in signal.detail


def test_capitulation_bar_on_off_and_unknown() -> None:
    closes = [*_decline(39), 101.5]  # prior close 100.5
    on = Series(make(closes, opens={-1: 98.0}, volumes={-1: 2_500_000}))
    signal = capitulation_bar(on)
    assert signal.on is True
    assert "closed green" in signal.detail

    # Same gap, ordinary volume.
    assert capitulation_bar(Series(make(closes, opens={-1: 98.0}))).on is False
    # Gap down that closes red.
    red = [*_decline(39), 97.0]
    assert (
        capitulation_bar(Series(make(red, opens={-1: 98.0}, volumes={-1: 2_500_000}))).on is False
    )
    # No volume stored: unknown, never off.
    no_volume = [b.model_copy(update={"volume": None}) for b in make(closes)]
    assert capitulation_bar(Series(no_volume)).on is None


def test_higher_low_signal_reads_the_index() -> None:
    assert higher_low(Series(make(TURNED))).on is True
    falling = higher_low(Series(make(FALLING)))
    assert falling.on is False
    assert "made the last session" in falling.detail
    assert higher_low(Series(make([100.0] * 5))).on is None


def _universe(n: int = 25, days: int = 30, **day_moves: float) -> list[Series]:
    """`n` names, half rising and half falling each day, except the days named
    in `day_moves` ('m3' = three sessions back) where every name moves alike."""
    forced = {int(k[1:]): v for k, v in day_moves.items()}
    out: list[Series] = []
    for j in range(n):
        closes = [100.0]
        for i in range(1, days):
            back = days - 1 - i
            step = forced.get(back, 1.0 if (i + j) % 2 else -1.0)
            closes.append(closes[-1] + step)
        out.append(Series(make(closes)))
    return out


def test_breadth_thrust_needs_a_washout_then_a_thrust() -> None:
    spy = Series(make([100.0] * 30))
    on = breadth_thrust(spy, _universe(m3=-2.0, m0=2.0))
    assert on.on is True
    assert "washout 0% up" in on.detail and "thrust 100%" in on.detail

    washed = breadth_thrust(spy, _universe(m3=-2.0))
    assert washed.on is False and "no 80% day since" in washed.detail
    # A thrust with no washout before it is just a good day.
    assert breadth_thrust(spy, _universe(m0=2.0)).on is False
    # Too few names to call it breadth.
    assert breadth_thrust(spy, _universe(n=5, m3=-2.0, m0=2.0)).on is None


def _leaders(beaten_bounce: float, strong_bounce: float) -> list[Series]:
    out: list[Series] = []
    for j in range(25):
        closes = [100.0] * 18
        slope = -1.5 if j < 5 else (0.5 if j >= 20 else -0.2)
        for _ in range(11):
            closes.append(closes[-1] + slope)
        bounce = beaten_bounce if j < 5 else (strong_bounce if j >= 20 else 0.5)
        closes.append(closes[-1] * (1 + bounce / 100))
        out.append(Series(make(closes)))
    return out


def test_leadership_flip_compares_the_hardest_hit_with_the_strongest() -> None:
    up_day = Series(make([*[100.0] * 29, 101.0]))
    on = leadership_flip(up_day, _leaders(beaten_bounce=4.0, strong_bounce=0.5))
    assert on.on is True
    assert "hardest-hit fifth +4.0%" in on.detail
    assert leadership_flip(up_day, _leaders(beaten_bounce=0.2, strong_bounce=2.0)).on is False
    # Not an up day for the index: nothing to read.
    down_day = Series(make([*[100.0] * 29, 99.0]))
    off = leadership_flip(down_day, _leaders(4.0, 0.5))
    assert off.on is False and "not an up day" in off.detail


def test_rsi_divergence_on_a_lower_low_with_less_force() -> None:
    # A fast drop, a bounce, then a slow grind to a slightly lower low.
    base = [100.0 + 0.5 * math.sin(i) for i in range(40)]
    fast = [100 - 3.0 * (k + 1) for k in range(5)]  # to 85
    bounce = [87.0, 89.0, 91.0, 92.0, 92.5]
    grind = [92.0, 91.0, 91.5, 90.0, 90.5, 88.5, 89.0, 87.0, 87.5, 86.0, 86.4, 84.6]
    signal = rsi_divergence(Series(make(base + fast + bounce + grind)))
    assert signal.on is True, signal.detail
    assert "RSI" in signal.detail

    # No lower low: off, and it says so.
    higher = rsi_divergence(Series(make(base + fast + bounce + [91.0] * 12)))
    assert higher.on is False and "no lower low" in higher.detail
    assert rsi_divergence(Series(make([100.0] * 10))).on is None


def test_market_state_counts_what_is_on_and_what_is_known() -> None:
    state = market_state(Series(make(TURNED)), None, [])
    assert state.as_of == END
    assert len(state.signals) == 6
    by_key = {s.key: s for s in state.signals}
    assert by_key["vix_rollover"].on is None  # no VIX bars
    assert by_key["breadth_thrust"].on is None  # no names
    assert by_key["higher_low"].on is True
    assert state.on_count == sum(1 for s in state.signals if s.on)
    assert state.known_count < 6
    assert state.spy_vs_200_pct is not None and state.spy_vs_200_pct < 0
    assert market_state(None, None, []).signals == []


# -------------------------------------------------------------------- service


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _seed(conn, provider: FakeProvider, **series: list[float]) -> None:
    for symbol, closes in series.items():
        provider.add_symbol(symbol, price=str(closes[-1]))
        seed(conn, symbol, make(closes))


def _home(conn, provider: FakeProvider, engine: EngineService, sleeve: bool = True) -> None:
    """A live engine on four names with a two-slot human sleeve, plus the index
    and the VIX it reads the market from."""
    _seed(conn, provider, TURN=TURNED, WAIT=FALLING, FLAT=FLAT, HELD=TURNED)
    seed(conn, "SPY", make(FALLING))
    seed(conn, "^VIX", make([30.0] * len(FALLING)))
    _live(engine, ["TURN", "WAIT", "FLAT", "HELD"])
    if sleeve:
        HumanService(conn, provider).set(NOW, slots=2, capital=D("10000"))


def test_brief_lists_the_turned_name_with_the_plan_a_buy_would_get(conn, provider, engine) -> None:
    _home(conn, provider, engine)
    brief = HumanBriefService(engine).brief(END)

    assert brief.market_open and brief.state.as_of == END
    assert (brief.universe, brief.screened, brief.stretched) == (4, 4, 3)
    assert [c.symbol for c in brief.candidates] == ["HELD", "TURN"] or [
        c.symbol for c in brief.candidates
    ] == ["TURN", "HELD"]
    turn = next(c for c in brief.candidates if c.symbol == "TURN")
    assert turn.trigger == "higher_low"
    assert turn.close == D("83.5")
    assert turn.stop_price < turn.close < turn.target_price
    # Sized to one human slot: 10,000 across two slots.
    assert turn.quantity is not None
    assert abs(turn.quantity * turn.close - D("5000")) < D("0.01")
    # Risk is what the slot loses at the stop (the shown stop is rounded to the cent).
    assert turn.risk is not None
    assert abs(turn.risk - turn.quantity * (turn.close - turn.stop_price)) < D("0.50")
    # The target sits target_r (2) stop distances above the entry.
    assert abs((turn.target_price - turn.close) - 2 * (turn.close - turn.stop_price)) < D("0.02")
    assert "below its 200-session average" in turn.reason
    assert turn.stretch_4y_pct is None  # 221 bars: the 4-year average is unknown, not zero

    assert [w.symbol for w in brief.watching] == ["WAIT"]
    assert brief.sleeve.configured and (brief.sleeve.slots, brief.sleeve.free) == (2, 2)
    assert brief.sleeve.size == D("5000.00")
    assert brief.caveats[0] == MEASURED_NOTE
    assert brief.hold_until == sessions_ahead(END, brief.hold_sessions)


def test_a_name_that_already_has_an_owner_is_set_aside(conn, provider, engine) -> None:
    _home(conn, provider, engine)
    _filled_position(engine, conn, "HELD")
    brief = HumanBriefService(engine).brief(END)
    assert [c.symbol for c in brief.candidates] == ["TURN"]
    assert [(x.symbol, x.why) for x in brief.excluded] == [("HELD", "held by the engine")]


def test_earnings_inside_the_hold_are_flagged(conn, provider, engine) -> None:
    _home(conn, provider, engine)
    instruments = InstrumentRepo(conn)
    turn, held = instruments.get_by_symbol("TURN"), instruments.get_by_symbol("HELD")
    assert turn is not None and held is not None
    EarningsRepo(conn).upsert(turn.id, [EarningsDate(date=END + timedelta(days=4))], today=END)
    EarningsRepo(conn).upsert(held.id, [EarningsDate(date=END + timedelta(days=60))], today=END)
    by_symbol = {c.symbol: c for c in HumanBriefService(engine).brief(END).candidates}
    assert by_symbol["TURN"].earnings_in_window is True
    assert by_symbol["TURN"].earnings_on == END + timedelta(days=4)
    assert by_symbol["HELD"].earnings_in_window is False
    assert by_symbol["HELD"].earnings_on == END + timedelta(days=60)


def test_a_broken_series_is_set_aside_with_the_reason(conn, provider, engine) -> None:
    _home(conn, provider, engine)
    seed(conn, "WAIT", make([100.0] * 205 + [40.0] * 11 + [41, 42, 43, 41.5, 43.5]))
    brief = HumanBriefService(engine).brief(END)
    assert "WAIT" not in [w.symbol for w in brief.watching]
    (aside,) = [x for x in brief.excluded if x.symbol == "WAIT"]
    assert "price break" in aside.why and "-60%" in aside.why


def test_without_a_sleeve_the_levels_are_still_shown_unsized(conn, provider, engine) -> None:
    _home(conn, provider, engine, sleeve=False)
    brief = HumanBriefService(engine).brief(END)
    assert not brief.sleeve.configured
    turn = next(c for c in brief.candidates if c.symbol == "TURN")
    assert turn.quantity is None and turn.risk is None
    assert turn.stop_price < turn.close < turn.target_price


def test_a_date_with_no_session_reads_the_last_one_and_says_so(conn, provider, engine) -> None:
    _home(conn, provider, engine)
    brief = HumanBriefService(engine).brief(END + timedelta(days=2))
    assert brief.state.as_of == END
    assert not brief.market_open
    assert any("No session stored" in c for c in brief.caveats)
    assert [c.symbol for c in brief.candidates]  # still a read, just not one to send


def test_brief_writes_nothing(conn, provider, engine) -> None:
    _home(conn, provider, engine)
    before = conn.execute("SELECT count(*) FROM order_intent").fetchone()
    HumanBriefService(engine).brief(END)
    assert conn.execute("SELECT count(*) FROM order_intent").fetchone() == before


# ------------------------------------------------------------------- message


def test_message_carries_every_block_and_passes_the_privacy_guard(
    conn, provider, engine, monkeypatch, tmp_path
) -> None:
    from trd.notify.privacy import guard

    monkeypatch.setenv("TRD_PRIVATE_TERMS", str(tmp_path / "absent.txt"))
    _home(conn, provider, engine)
    _filled_position(engine, conn, "HELD")
    text = human_brief_message(HumanBriefService(engine).brief(END))
    assert text.startswith("🧭 <b>Human-slot brief</b> · Fri Mar 13")
    for part in (
        "Bounce signals on:",
        "<b>Market state</b>",
        "<b>Higher low</b>",
        "<b>Stretched and turned</b> · 3 of 4 names stretched",
        "• <b>TURN</b> 83.50",
        "stop ",
        "<b>Stretched, not turned</b>: WAIT",
        "<b>Set aside</b>: HELD",
        "Room for <b>2</b> of 2 human slots",
        "A screen, not a recommendation",
        "trd engine human buy SYM",
    ):
        assert part in text, part
    assert "brokerage" not in text  # the account is never named
    guard(text)


def test_message_with_nothing_turned_and_no_sleeve(conn, provider, engine) -> None:
    _seed(conn, provider, WAIT=FALLING, FLAT=FLAT)
    seed(conn, "SPY", make(FALLING))
    _live(engine, ["WAIT", "FLAT"])
    text = human_brief_message(HumanBriefService(engine).brief(END))
    assert "None has turned yet." in text
    assert "No human slots on this engine." in text


# ---------------------------------------------------------------------- study


def _dipper(shift: int, n: int = 330) -> list[float]:
    """A wobbling name with one 20% sell-off and a turn, `shift` bars along."""
    closes = [100.0 + 0.8 * math.sin(i / 2.5 + shift) for i in range(n)]
    start = 215 + shift
    for k in range(10):
        closes[start + k] = 98 - 2 * k
    for k, value in enumerate([81, 82, 83, 81.5, 83.5]):
        closes[start + 10 + k] = value
    for i in range(start + 15, n):
        closes[i] = 84 + 0.15 * (i - start - 15) + 0.5 * math.sin(i / 2.0)
    return closes


def _study_home(conn, provider, engine) -> None:
    series = {f"D{j}": _dipper(8 * j) for j in range(4)}
    series |= {f"W{j}": [100.0 + math.sin(i / 3.0 + j) for i in range(330)] for j in range(6)}
    _seed(conn, provider, **series)
    _live(engine, list(series))


def test_study_pairs_every_firing_with_random_names_on_the_same_day(conn, provider, engine) -> None:
    _study_home(conn, provider, engine)
    report = HumanBriefService(engine).study(samples=3, seed=1)
    assert report.names == 10
    assert report.rule.count >= 4  # each dipper turns at least once
    assert report.control.count == report.rule.count * 3
    assert report.edge.count == report.rule.count
    assert report.edge.mean is not None and report.edge.se is not None
    assert [h.count for h in report.halves] == [
        report.rule.count // 2,
        report.rule.count - report.rule.count // 2,
    ]
    assert {p.label for p in report.by_trigger} == {"reclaim", "higher_low"}
    assert sum(p.count for p in report.by_trigger) == report.rule.count
    assert report.horizon == 10
    assert report.first is not None and report.last is not None and report.first <= report.last
    # 330 bars: nobody has the 4-year average, and the report says so.
    assert report.four_year == []
    assert any("4-year" in c for c in report.caveats)
    assert isinstance(report.significant, bool)


def test_study_is_reproducible_and_the_seed_matters_only_to_the_control(
    conn, provider, engine
) -> None:
    _study_home(conn, provider, engine)
    service = HumanBriefService(engine)
    assert service.study(samples=2, seed=5) == service.study(samples=2, seed=5)
    assert service.study(samples=2, seed=5).rule == service.study(samples=2, seed=6).rule


def test_study_with_no_firing_says_so(conn, provider, engine) -> None:
    series = {f"W{j}": [100.0 + math.sin(i / 3.0 + j) for i in range(330)] for j in range(3)}
    _seed(conn, provider, **series)
    _live(engine, list(series))
    report = HumanBriefService(engine).study()
    assert report.rule.count == 0 and not report.significant
    assert report.caveats[0] == "The rule never fired in the stored history."


# ------------------------------------------------------------------------ CLI


def _cli_home(cli_env: FakeProvider, monkeypatch) -> None:
    import os

    conn = connect(Path(os.environ["TRD_HOME"]) / "trd.duckdb")
    try:
        _home(conn, cli_env, EngineService(conn, cli_env))
    finally:
        conn.close()


def test_cli_brief_json_is_the_whole_model(cli_env, monkeypatch) -> None:
    _cli_home(cli_env, monkeypatch)
    result = runner.invoke(app, ["engine", "human", "brief", "--date", END.isoformat(), "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert set(body) == {"message", "sent", "brief"}
    assert body["sent"] is False
    assert body["brief"]["market_open"] is True
    assert {c["symbol"] for c in body["brief"]["candidates"]} == {"TURN", "HELD"}
    assert len(body["brief"]["state"]["signals"]) == 6
    assert body["brief"]["sleeve"]["free"] == 2


def test_cli_brief_renders_for_a_person(cli_env, monkeypatch) -> None:
    _cli_home(cli_env, monkeypatch)
    result = runner.invoke(app, ["engine", "human", "brief", "--date", END.isoformat()])
    assert result.exit_code == 0, result.output
    for part in ("Human-slot brief", "Market state", "Stretched and turned", "TURN", "WAIT"):
        assert part in result.output, part


def test_cli_notify_goes_to_the_private_chat(cli_env, monkeypatch, _no_telegram) -> None:
    _cli_home(cli_env, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")  # the channel: never used here
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    result = runner.invoke(
        app, ["engine", "human", "brief", "--date", END.isoformat(), "--notify", "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["sent"] is True
    assert [m["chat_id"] for m in _no_telegram] == ["555"]
    assert _no_telegram[0]["text"] == body["message"]


def test_cli_notify_refuses_without_a_private_chat(cli_env, monkeypatch, _no_telegram) -> None:
    _cli_home(cli_env, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    result = runner.invoke(
        app, ["engine", "human", "brief", "--date", END.isoformat(), "--notify", "--json"]
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["error"]
    assert _no_telegram == []


def test_cli_sends_nothing_on_a_date_with_no_session(cli_env, monkeypatch, _no_telegram) -> None:
    _cli_home(cli_env, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    weekend = (END + timedelta(days=1)).isoformat()
    result = runner.invoke(
        app, ["engine", "human", "brief", "--date", weekend, "--notify", "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["sent"] is False and body["brief"]["market_open"] is False
    assert _no_telegram == []


def test_cli_study_json_and_table(cli_env, monkeypatch) -> None:
    _cli_home(cli_env, monkeypatch)
    result = runner.invoke(app, ["engine", "human", "study", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert {"horizon", "rule", "control", "edge", "halves", "significant", "caveats"} <= set(body)
    assert runner.invoke(app, ["engine", "human", "study"]).exit_code == 0
    bad = runner.invoke(app, ["engine", "human", "study", "--samples", "0", "--json"])
    assert bad.exit_code != 0 and json.loads(bad.output)["error"]
