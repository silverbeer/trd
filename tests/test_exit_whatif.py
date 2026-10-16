"""Exit what-if (SB-1343): replay exit params on the entries the engine really took.

Every fixture is a day-engine trade entered at 100 with an ATR of 5, so the live
stop sits 2 x 5 = 10 below — 1R = 10 and every close below reads directly as an
R-multiple of the LIVE trade, which is the unit every arm is reported in.
"""

import json
import os
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.cli.app import app
from trd.engine.bars import BarSource
from trd.engine.exits import DEFAULT_EXIT_PARAMS
from trd.errors import TrdError
from trd.models import Bar, EnginePosition, IntradayBar, PositionStatus
from trd.services import EngineService
from trd.services.backtest import FillMode
from trd.services.exit_whatif import compare, parse_variants, replay_exit

runner = CliRunner()

FIVE = BarSource.stamper("5m")
DAY = {**DEFAULT_EXIT_PARAMS, "flat_at_minute": 1555.0}
ENTRY = Decimal("100")
ATR = Decimal("5")  # x stop_atr_mult 2 = a 10-wide stop: 1R = 10
SESSION = datetime(2026, 8, 5)
OPENED = datetime(2026, 8, 5, 10, 0, 30)  # inside the 10:00 bucket


def ibar(hh: int, mm: int, close: str, low: str | None = None, high: str | None = None):
    c = Decimal(close)
    return IntradayBar(
        ts=SESSION.replace(hour=hh, minute=mm),
        open=c,
        high=Decimal(high) if high else c,
        low=Decimal(low) if low else c,
        close=c,
        volume=1_000,
    )


def session(closes: dict[tuple[int, int], str], default: str = "100") -> list[Bar]:
    """A whole 09:30-15:55 session of 5-minute bars, flat at `default` except
    where `closes` says otherwise."""
    bars: list[Bar] = []
    t = SESSION.replace(hour=9, minute=30)
    while t.hour < 16:
        bars.append(ibar(t.hour, t.minute, closes.get((t.hour, t.minute), default)))
        t += timedelta(minutes=5)
    return bars


def live(
    opened: datetime = OPENED,
    exit_price: str = "89",
    rule: str | None = "stop",
    pid: int = 1,
) -> EnginePosition:
    return EnginePosition(
        id=pid,
        account_id=1,
        instrument_id=1,
        strategy="momentum",
        opened_at=opened,
        entry_price=ENTRY,
        quantity=Decimal("1"),
        stop_price=ENTRY - ATR * 2,
        target_price=ENTRY + ATR * 4,
        atr_at_entry=ATR,
        trail_high=ENTRY,
        status=PositionStatus.CLOSED,
        closed_at=opened + timedelta(minutes=10),
        exit_price=Decimal(exit_price),
        closed_quantity=Decimal("1"),
        booked_pnl=Decimal(exit_price) - ENTRY,
        exit_rule=rule,
    )


# ------------------------------------------------------------------- replay


def test_the_baseline_replay_reproduces_the_live_stop() -> None:
    """Same params, same entry, same bars: the replay stops out where the live
    trade did. The entry's own bucket is skipped even though it printed 80 —
    the fill was inside it, at its close, so that low was never the trade's."""
    bars = session({(10, 0): "80", (10, 5): "95", (10, 10): "89"})
    out = replay_exit(live(), bars, FIVE, DAY, FillMode.CLOSE)
    assert out is not None
    assert out.rule == "stop"
    assert out.exit_price == Decimal("89")
    assert out.r == Decimal("-1.1")
    assert out.closed_at == SESSION.replace(hour=10, minute=10)


def test_a_wider_stop_is_scored_in_the_live_trades_R() -> None:
    """stop_atr_mult=3 moves the stop to 85, so 89 no longer fires and the trade
    is flattened at the bell at 92. That is -0.8 of the LIVE R (10) — not -0.53
    of its own wider R, which would make every wide-stop row look smaller."""
    bars = session({(10, 10): "89"}, default="92")
    out = replay_exit(live(), bars, FIVE, {**DAY, "stop_atr_mult": 3.0}, FillMode.CLOSE)
    assert out is not None
    assert out.rule == "session_close"
    assert out.r == Decimal("-0.8")


def test_a_trade_with_no_ending_in_the_stored_bars_is_not_scored() -> None:
    """A position the bars never close — the series stops mid-session — has no
    result, and inventing one at the last close would be a fill that never was."""
    bars: list[Bar] = [ibar(10, 0, "100"), ibar(10, 5, "101"), ibar(10, 10, "102")]
    assert replay_exit(live(), bars, FIVE, DAY, FillMode.CLOSE) is None


def test_a_time_exit_reads_fractional_sessions_on_a_day_engine() -> None:
    """max_sessions=0.25 resolves to five-minute bars — about an hour and a half
    — not to a quarter of one bar, and the trade leaves on the time rule."""
    bars = session({}, default="101")
    out = replay_exit(live(), bars, FIVE, {**DAY, "max_sessions": 0.25}, FillMode.CLOSE)
    assert out is not None
    assert out.rule == "time"
    assert out.r == Decimal("0.1")


# ------------------------------------------------------------------ compare


def test_variants_are_paired_against_the_baseline_on_the_same_trades() -> None:
    """Two trades. A nearer target books the one that ran to +1.5R and leaves
    the stop-out alone: one trade changed, by +1.5R - (bell at +0.5R) = +1.0R."""
    runner_bars = session({(10, 30): "115", (11, 0): "105"}, default="105")
    stopped = session({(10, 10): "89"})
    trades = [
        (live(pid=1, exit_price="105", rule="session_close"), runner_bars),
        (live(pid=2), stopped),
    ]
    actual, base, arms, excluded, gap, same = compare(
        trades, FIVE, DAY, [{"target_r": 1.5}], FillMode.CLOSE
    )
    assert excluded == {}
    assert base.trades == 2
    assert base.rules == {"session_close": 1, "stop": 1}
    (arm,) = arms
    assert arm.rules == {"target": 1, "stop": 1}
    assert arm.changed == 1
    assert arm.diff_r == pytest.approx(0.5)  # +1.0R on one of two trades
    assert actual.expectancy_r == pytest.approx((0.5 - 1.1) / 2)
    assert gap == pytest.approx(0.0)
    assert same == 100.0


def test_an_unchanged_variant_reports_zero_difference_not_noise() -> None:
    """A param that does not alter a single exit must read as exactly zero, with
    nothing changed — the trail_activate_r=0.5 case on the live config, where
    the trail cannot rise past the stop before +0.5R anyway."""
    trades = [(live(), session({(10, 10): "89"}))]
    _a, _b, (arm,), _x, _g, _s = compare(
        trades, FIVE, DAY, [{"trail_activate_r": 0.5}], FillMode.CLOSE
    )
    assert arm.diff_r == 0
    assert arm.changed == 0
    assert arm.significant is None  # one trade has no standard error


def test_a_trade_any_arm_cannot_close_leaves_every_arm() -> None:
    """Paired means paired: a trade only the baseline could score would put the
    rows on different sets of trades."""
    short: list[Bar] = [ibar(10, 0, "100"), ibar(10, 5, "100"), ibar(10, 10, "89")]
    trades = [(live(), short)]
    _a, base, (arm,), excluded, _g, _s = compare(
        trades, FIVE, DAY, [{"stop_atr_mult": 3.0}], FillMode.CLOSE
    )
    assert base.trades == arm.trades == 0
    assert excluded == {"a variant ran past the stored bars": 1}


# ------------------------------------------------------------------- params


def test_each_value_is_its_own_row_and_each_flag_its_own_axis() -> None:
    assert parse_variants(["max_sessions=0.25,0.5", "target_r=1.5"]) == [
        {"max_sessions": 0.25},
        {"max_sessions": 0.5},
        {"target_r": 1.5},
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("entry_delay_minutes=10", "which trades exist"),
        ("regime_sma=100", "which trades exist"),
        ("trail_mult=2", "Unknown exit param"),
        ("target_r=abc", "not a number"),
        ("target_r=-1", "cannot be negative"),
        ("target_r", "Expected KEY=VALUE"),
    ],
)
def test_params_a_what_if_cannot_honour_are_refused(raw: str, message: str) -> None:
    """An entry knob would replay the baseline under a new name; a typo would do
    the same. Both are refused rather than reported as a result."""
    with pytest.raises(TrdError, match=message):
        parse_variants([raw])


# ---------------------------------------------------------------------- CLI


def test_cli_emits_json_and_counts_what_it_left_out(cli_env: FakeProvider, tmp_path) -> None:
    """A trade opened on the newest bar has nothing after it to replay: it is
    counted as left out, never scored and never silently dropped."""
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

    os.environ["TRD_HOME"] = str(home)
    try:
        result = runner.invoke(app, ["engine", "what-if", "-p", "target_r=1.5", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["trades"] == 0
        assert payload["excluded"] == {"no ending in stored bars": 1}
        assert [v["label"] for v in payload["variants"]] == ["target_r=1.5"]

        refused = runner.invoke(app, ["engine", "what-if", "-p", "entry_delay_minutes=5", "--json"])
        assert refused.exit_code != 0
        assert json.loads(refused.output)["error"]
    finally:
        os.environ.pop("TRD_HOME", None)
