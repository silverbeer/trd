"""Rotation study (SB-1537): what swapping the coldest holding for a blocked signal did.

Hand-built daily bars so every number is known by construction. Each bar's high
and low sit 1 either side of its close, so ATR(14) is 2 on a flat series and the
stop plan_entry would set (2 x ATR) is 4 below the entry: 1R = 4, target (2R) = 8.
"""

import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from typer.testing import CliRunner

from trd.engine.bars import BarSource
from trd.models import (
    DailyBar,
    EnginePosition,
    EngineSignal,
    Instrument,
    InstrumentType,
    PositionStatus,
)
from trd.services.rotation import (
    Holding,
    coldest,
    managed_return,
    measure_rotations,
    summarize,
)

DAILY = BarSource.stamper("1d")
START = date(2026, 6, 1)
PARAMS = {"stop_atr_mult": 2.0, "target_r": 2.0, "indicator_grace_sessions": 3.0}
DECIDE = 20  # the bar index the blocked signal fires on


def day(i: int) -> date:
    return START + timedelta(days=i)


def bars(closes: list[float]) -> list[DailyBar]:
    return [
        DailyBar(
            date=day(i),
            open=Decimal(str(c)),
            high=Decimal(str(c + 1)),
            low=Decimal(str(c - 1)),
            close=Decimal(str(c)),
            volume=1_000,
        )
        for i, c in enumerate(closes)
    ]


def inst(n: int, symbol: str) -> Instrument:
    return Instrument(id=n, symbol=symbol, type=InstrumentType.STOCK)


def held(
    pid: int, iid: int, opened: int, entry: str = "100", closed: tuple[int, str] | None = None
) -> EnginePosition:
    e = Decimal(entry)
    return EnginePosition(
        id=pid,
        account_id=1,
        instrument_id=iid,
        strategy="momentum",
        opened_at=datetime.combine(day(opened), time(10, 0)),
        entry_price=e,
        quantity=Decimal("1"),
        stop_price=e - 10,
        target_price=e + 20,
        atr_at_entry=Decimal("5"),
        trail_high=e,
        status=PositionStatus.CLOSED if closed else PositionStatus.OPEN,
        closed_at=datetime.combine(day(closed[0]), time(15, 0)) if closed else None,
        exit_price=Decimal(closed[1]) if closed else None,
    )


def signal(n: int, iid: int, at: int, score: float = 0.5, acted: bool = False) -> EngineSignal:
    return EngineSignal(
        id=n,
        instrument_id=iid,
        strategy="pullback",
        bar_ts=datetime.combine(day(at), time.min),
        fired_at=datetime.combine(day(at), time(11, 0)),
        price=Decimal("100"),
        score=score,
        reason="test",
        acted=acted,
    )


FLAT = [100.0] * 30


def test_managed_return_takes_whichever_level_is_touched_first() -> None:
    entry, risk = Decimal("100"), Decimal("4")
    up = bars([101, 104, 108])  # 108 + 1 high reaches the 108 target on bar 3
    assert managed_return(up, entry, risk, Decimal(2)) == (Decimal("0.08"), "target")
    down = bars([99, 96])  # bar 2 low 95 is under the 96 stop
    assert managed_return(down, entry, risk, Decimal(2)) == (Decimal("-0.04"), "stop")
    flat = bars([101, 102])
    assert managed_return(flat, entry, risk, Decimal(2)) == (Decimal("0.02"), "horizon")


def test_coldest_skips_the_grace_period_and_trades_near_target() -> None:
    def h(pid: int, r: str, sessions: int) -> Holding:
        return Holding(
            position_id=pid,
            symbol=f"S{pid}",
            strategy="x",
            sessions_held=sessions,
            r_now=Decimal(r),
            mark=Decimal(1),
        )

    picks = [h(1, "-0.9", 1), h(2, "-0.2", 5), h(3, "1.6", 5), h(4, "0.4", 4)]
    # 1 is coldest but still inside its grace; 3 is within 0.5R of a 2R target.
    pick = coldest(picks, grace_bars=3, target_r=Decimal(2))
    assert pick is not None and pick.position_id == 2
    assert coldest([h(1, "-0.9", 1)], grace_bars=3, target_r=Decimal(2)) is None


def _book(cand: list[float], cold: list[float], warm: list[float]):
    instruments = {1: inst(1, "CAND"), 2: inst(2, "COLD"), 3: inst(3, "WARM")}
    positions = [(held(10, 2, opened=5), instruments[2]), (held(11, 3, opened=5), instruments[3])]
    return instruments, positions, {1: bars(cand), 2: bars(cold), 3: bars(warm)}


def test_a_full_book_swap_is_priced_against_the_coldest_holding() -> None:
    # Candidate flat at 100 to the decision, then runs to its 108 target.
    cand = [*FLAT[:21], 104, 108, 108, 108, 108]
    # COLD sits at 95 (-0.5R) at the decision and drifts to 90 by the horizon.
    cold = FLAT[:15] + [95] * 6 + [94, 93, 92, 91, 90]
    warm = FLAT[:15] + [110] * 11  # +1R: warmer, so not the one sold
    instruments, positions, series = _book(cand, cold, warm)
    rows, blocked = measure_rotations(
        [(signal(1, 1, DECIDE), instruments[1])],
        positions,
        series,
        DAILY,
        PARAMS,
        max_positions=2,
        horizon=5,
        cost_pct=Decimal("0.10"),
    )
    assert blocked == 1
    (row,) = rows
    assert row.status == "measured"
    assert row.sold is not None and row.sold.symbol == "COLD"
    assert row.sold.r_now == Decimal("-0.5")
    assert row.candidate_exit == "target"
    assert row.candidate_pct == Decimal("8.00")
    # 95 -> 90 is -5.263...%
    assert row.keep_pct is not None and round(row.keep_pct, 3) == Decimal("-5.263")
    assert row.edge_pct is not None and round(row.edge_pct, 3) == Decimal("13.163")


def test_a_holding_that_closed_inside_the_horizon_is_kept_at_its_exit() -> None:
    cand = FLAT[:21] + [100.5] * 5
    cold = FLAT[:15] + [95] * 11
    warm = FLAT[:15] + [110] * 11
    instruments, _, series = _book(cand, cold, warm)
    # COLD actually exited on bar 22 at 99: keeping it made +4.21% from 95.
    positions = [
        (held(10, 2, opened=5, closed=(22, "99")), instruments[2]),
        (held(11, 3, opened=5), instruments[3]),
    ]
    rows, _ = measure_rotations(
        [(signal(1, 1, DECIDE), instruments[1])], positions, series, DAILY, PARAMS, 2, 5
    )
    assert rows[0].keep_pct is not None and round(rows[0].keep_pct, 2) == Decimal("4.21")
    assert rows[0].candidate_exit == "horizon"


def test_only_full_book_signals_count_and_one_per_session() -> None:
    cand = FLAT[:21] + [101] * 5
    instruments, positions, series = _book(cand, FLAT[:15] + [95] * 11, FLAT[:15] + [110] * 11)
    signals = [
        (signal(1, 1, DECIDE, score=0.3), instruments[1]),
        (signal(2, 1, DECIDE, score=0.9), instruments[1]),
        (signal(3, 1, DECIDE, score=0.99, acted=True), instruments[1]),
    ]
    rows, blocked = measure_rotations(signals, positions, series, DAILY, PARAMS, 2, 5)
    assert blocked == 1 and [r.signal_id for r in rows] == [2]
    # Three slots and two held: the book was not full, nothing to measure.
    rows, blocked = measure_rotations(signals, positions, series, DAILY, PARAMS, 3, 5)
    assert (rows, blocked) == ([], 0)


def test_a_horizon_still_filling_in_is_pending_not_measured() -> None:
    instruments, positions, series = _book(FLAT[:23], FLAT[:15] + [95] * 8, FLAT[:15] + [110] * 8)
    rows, _ = measure_rotations(
        [(signal(1, 1, DECIDE), instruments[1])], positions, series, DAILY, PARAMS, 2, 5
    )
    assert rows[0].status == "pending" and rows[0].edge_pct is None
    overall, _, _ = summarize(rows)
    assert overall.n == 0


def test_no_eligible_holding_inside_grace() -> None:
    instruments = {1: inst(1, "CAND"), 2: inst(2, "NEW")}
    positions = [(held(10, 2, opened=DECIDE - 1), instruments[2])]
    rows, _ = measure_rotations(
        [(signal(1, 1, DECIDE), instruments[1])],
        positions,
        {1: bars(FLAT[:26]), 2: bars(FLAT[:26])},
        DAILY,
        PARAMS,
        1,
        5,
    )
    assert rows[0].status == "no_eligible"


def test_cli_reports_json_read_only(cli_env, tmp_path, monkeypatch) -> None:
    from tests.test_daily_report import _home
    from trd.cli.app import app

    home = _home(tmp_path, "swing", cli_env)
    monkeypatch.setenv("TRD_HOME", str(home))
    result = CliRunner().invoke(app, ["engine", "rotation", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["overall"]["label"] == "all"
    assert "caveats" in doc and doc["cost_pct"] == "0.1"
