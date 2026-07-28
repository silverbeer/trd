"""Backtest harness tests.

The harness is a driver around rules that are tested elsewhere, so these tests
aim at the driving: fill mechanics (gaps, intrabar touches, pessimistic
ordering), date alignment across unequal histories, the earnings blackout, the
day-mode refusal — and above all lookahead, the failure mode that looks like
success.
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import duckdb
import pytest

from trd.engine import DEFAULT_EXIT_PARAMS
from trd.errors import TrdError
from trd.models import DailyBar, StrategyStat
from trd.repos import InstrumentRepo, PriceRepo
from trd.services import BacktestService, EngineService
from trd.services.backtest import BacktestResult, FillMode, simulate
from trd.services.engine import plan_entry

from .conftest import FakeProvider
from .test_engine import make_bars

# ----------------------------------------------------------------- helpers


def bar(
    day: date, open_: float, high: float, low: float, close: float, volume: int = 1_000_000
) -> DailyBar:
    return DailyBar(
        date=day,
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
    )


def breakout_series() -> list[DailyBar]:
    """60 flat bars then a close above the 20-day high on 3x volume — the
    canonical breakout entry, firing on the last bar."""
    closes = [100.0] * 60 + [101.0]
    volumes = [1_000_000] * 60 + [3_000_000]
    return make_bars(closes, volumes=volumes)


def run(
    bars_by_symbol: dict[str, list[DailyBar]],
    *,
    strategies: list[str] | None = None,
    position_size: Decimal = Decimal("1000"),
    max_positions: int = 2,
    exit_params: dict[str, float] | None = None,
    **extra: Any,
) -> BacktestResult:
    return simulate(
        bars_by_symbol,
        strategies=strategies or ["breakout"],
        position_size=position_size,
        max_positions=max_positions,
        exit_params=exit_params or dict(DEFAULT_EXIT_PARAMS),
        **extra,
    )


def entry_levels(series: list[DailyBar]) -> tuple[Decimal, Decimal, Decimal]:
    """(entry, stop, target) for a fill on the last bar — via the same
    plan_entry the harness itself uses."""
    plan, skip = plan_entry(series, Decimal("1000"), dict(DEFAULT_EXIT_PARAMS))
    assert plan is not None, skip
    return series[-1].close, plan.stop_price, plan.target_price


# ------------------------------------------------------------- fill mechanics


def test_entry_fills_at_the_signal_bars_close():
    series = breakout_series()
    result = run({"AAA": series})
    assert result.open_at_end == 1
    assert not result.trades  # nothing has exited yet


def test_intrabar_stop_touch_fills_exactly_at_the_stop():
    series = breakout_series()
    _entry, stop, _target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 101.0, 101.5, float(stop) - 0.5, 100.9)]

    result = run({"AAA": series})
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.rule == "stop"
    assert trade.exit_price == stop
    assert trade.r_multiple == Decimal(-1)


def test_gap_through_stop_fills_at_the_open_not_the_stop():
    """The earnings-gap case: the open is already through the level, so the
    fill is the open and the trade loses more than the 1R it claimed."""
    series = breakout_series()
    _entry, stop, _target = entry_levels(series)
    gap_open = float(stop) - 5
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, gap_open, gap_open + 1, gap_open - 1, gap_open + 0.5)]

    result = run({"AAA": series})
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.rule == "stop"
    assert trade.exit_price == Decimal(str(gap_open))
    assert trade.r_multiple is not None and trade.r_multiple < Decimal(-1)


def test_target_touch_fills_exactly_at_the_target():
    series = breakout_series()
    _entry, _stop, target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 102.0, float(target) + 1, 101.5, 103.0)]

    result = run({"AAA": series})
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.rule == "target"
    assert trade.exit_price == target
    assert trade.r_multiple == Decimal(2)


def test_stop_wins_when_one_bar_touches_both_levels():
    """OHLC cannot say which traded first, so the harness assumes the worst —
    the same capital-protection-first order the live rules run in."""
    series = breakout_series()
    _entry, stop, target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 101.0, float(target) + 1, float(stop) - 1, 101.0)]

    result = run({"AAA": series})
    assert len(result.trades) == 1
    assert result.trades[0].rule == "stop"
    assert result.trades[0].r_multiple == Decimal(-1)


def test_close_mode_misses_the_intrabar_wick():
    """A wick through the stop that closes back above it: intrabar stops out,
    close-only rides through. The two modes must actually differ."""
    series = breakout_series()
    _entry, stop, _target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    wick = [*series, bar(next_day, 101.0, 101.5, float(stop) - 0.5, 100.9)]

    intrabar = run({"AAA": wick}, fill=FillMode.INTRABAR)
    close = run({"AAA": wick}, fill=FillMode.CLOSE)
    assert len(intrabar.trades) == 1
    assert not close.trades
    assert close.open_at_end == 1


# ---------------------------------------------------------------- lookahead


def test_no_lookahead_prefix_runs_are_identical():
    """The trap this whole harness exists to avoid: altering the future must
    not change any decision already made."""
    series = breakout_series()
    _entry, _stop, target = entry_levels(series)
    prefix_end = series[-1].date
    day = prefix_end
    future = []
    for i, close in enumerate((102.0, 103.0, float(target) + 1, 104.0), start=1):
        day_i = prefix_end + timedelta(days=i)
        future.append(bar(day_i, close - 0.5, close + 0.5, close - 1, close))

    full = run({"AAA": [*series, *future]})
    prefix_only = run({"AAA": series})

    def entries(result):
        return [
            (t.symbol, t.strategy, t.entry_date, t.entry_price)
            for t in result.trades
            if t.entry_date <= day
        ]

    # The prefix run has an open, un-exited position; the full run closed it.
    # The *entries* made inside the prefix must match exactly either way.
    assert len(full.trades) == 1
    assert full.trades[0].entry_date <= day
    assert prefix_only.open_at_end == 1
    assert entries(full)[0][2] == day

    # Now rewrite the future entirely: crash instead of rally. The entry taken
    # at the prefix boundary must be unchanged in date, price, and size.
    crash = [bar(prefix_end + timedelta(days=i), 60.0, 61.0, 59.0, 60.0) for i in range(1, 5)]
    crashed = run({"AAA": [*series, *crash]})
    assert crashed.trades[0].entry_date == full.trades[0].entry_date
    assert crashed.trades[0].entry_price == full.trades[0].entry_price
    assert crashed.trades[0].quantity == full.trades[0].quantity


# ------------------------------------------------------- alignment and limits


def shifted_breakout(start: date) -> list[DailyBar]:
    """A breakout series whose dates start where the caller says, so two
    symbols with different history depths can fire on the same calendar day."""
    closes = [100.0] * 60 + [101.0]
    volumes = [1_000_000] * 60 + [3_000_000]
    out = []
    for i, close in enumerate(closes):
        out.append(
            bar(
                start + timedelta(days=i),
                close,
                close * 1.005,
                close * 0.995,
                close,
                volume=volumes[i],
            )
        )
    return out


def test_max_positions_holds_across_unequal_histories():
    """Two symbols, different history depths, breaking out on the same calendar
    date. A per-symbol index walk would misalign the days; the date walk keeps
    the position limit honest."""
    long_start = date(2024, 1, 1)
    short_start = date(2024, 1, 21)
    # 20 extra bars of history, then the same breakout re-dated so it lands on
    # the same calendar day as the short symbol's.
    long_series = [
        bar(long_start + timedelta(days=i), 100.0, 100.5, 99.5, 100.0) for i in range(20)
    ] + [
        b.model_copy(update={"date": long_start + timedelta(days=20 + i)})
        for i, b in enumerate(shifted_breakout(short_start))
    ]
    short_series = shifted_breakout(short_start)
    assert long_series[-1].date == short_series[-1].date

    result = run({"AAA": long_series, "BBB": short_series}, max_positions=1)
    assert result.open_at_end == 1

    both = run({"AAA": long_series, "BBB": short_series}, max_positions=2)
    assert both.open_at_end == 2


def test_one_position_per_symbol():
    """A second signal in a name already held is not a second position."""
    series = breakout_series()
    last = series[-1]
    # Another new 20-day high on heavy volume the very next day.
    next_day = last.date + timedelta(days=1)
    series = [*series, bar(next_day, 101.5, 102.5, 101.2, 102.4, volume=3_000_000)]
    result = run({"AAA": series}, max_positions=5)
    assert result.open_at_end == 1


# ------------------------------------------------------------------ blackout


def test_earnings_blackout_blocks_the_entry():
    series = breakout_series()
    signal_day = series[-1].date
    earnings = {"AAA": [signal_day + timedelta(days=2)]}

    blocked = run({"AAA": series}, earnings_by_symbol=earnings, earnings_blackout_days=3)
    assert blocked.open_at_end == 0
    assert blocked.blackout_blocked == 1

    taken = run({"AAA": series}, earnings_by_symbol=earnings, earnings_blackout_days=0)
    assert taken.open_at_end == 1


# ------------------------------------------------------------------ refusals


def test_day_mode_is_refused():
    params = dict(DEFAULT_EXIT_PARAMS, flat_at_minute=1555.0)
    with pytest.raises(TrdError, match="intraday clock"):
        run({"AAA": breakout_series()}, exit_params=params)


def test_unknown_strategy_is_refused():
    with pytest.raises(TrdError, match="Unknown strategies"):
        run({"AAA": breakout_series()}, strategies=["moon_phase"])


def test_empty_history_is_refused():
    with pytest.raises(TrdError, match="No price history"):
        run({})


# ----------------------------------------------------------- output contract


def test_stats_use_the_live_scorecard_shape_and_books_balance():
    series = breakout_series()
    _entry, _stop, target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 102.0, float(target) + 1, 101.5, 103.0)]

    result = run({"AAA": series})
    assert len(result.stats) == 1
    stat = result.stats[0]
    assert isinstance(stat, StrategyStat)
    assert stat.strategy == "breakout"
    assert stat.trades == 1 and stat.wins == 1
    assert stat.expectancy_r == Decimal(2)

    # Flat at the end, so ending equity is exactly start + realized P&L.
    assert result.open_at_end == 0
    assert result.end_value == result.start_value + result.trades[0].pnl
    assert result.caveat  # the number is an upper bound; the result must say so


def test_equity_curve_and_drawdown_sanity():
    series = breakout_series()
    _entry, stop, _target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 101.0, 101.5, float(stop) - 0.5, 100.9)]

    result = run({"AAA": series})
    dates = [p.date for p in result.equity]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates))
    assert result.max_drawdown_pct < 0  # the stop-out dented the curve
    assert result.end_value < result.start_value


def test_start_gates_trading_but_not_warmup():
    """Bars before `start` feed the indicators; the report and the equity curve
    begin where trading may. A breakout before the gate is not taken."""
    series = breakout_series()
    signal_day = series[-1].date
    late = signal_day + timedelta(days=5)
    quiet = [bar(signal_day + timedelta(days=i), 101.0, 101.4, 100.6, 101.0) for i in range(1, 10)]
    result = run({"AAA": [*series, *quiet]}, start=late)
    assert result.start == late
    assert result.equity[0].date >= late
    assert result.open_at_end == 0  # the pre-gate breakout was never taken


def test_warmup_shortfall_is_named_not_silent():
    short = {"AAA": make_bars([100.0] * 30)}
    result = run(short)
    assert result.skipped and "trd sync" in result.skipped[0]


# ----------------------------------------------------------- service loading


def test_service_runs_from_a_configured_engine(conn: duckdb.DuckDBPyConnection):
    provider = FakeProvider()
    provider.add_symbol("AAPL", price="101.00")
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAPL"], strategies=["breakout"])

    instrument = InstrumentRepo(conn).get_by_symbol("AAPL")
    assert instrument is not None
    series = breakout_series()
    _entry, _stop, target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 102.0, float(target) + 1, 101.5, 103.0)]
    PriceRepo(conn).upsert_daily(instrument.id, series)

    result = BacktestService(conn).run()
    assert len(result.trades) == 1
    assert result.trades[0].rule == "target"


def test_service_refuses_without_an_engine(conn: duckdb.DuckDBPyConnection):
    with pytest.raises(TrdError, match="engine init"):
        BacktestService(conn).run()


def test_service_symbols_override_requires_known_symbols(
    conn: duckdb.DuckDBPyConnection,
):
    provider = FakeProvider()
    provider.add_symbol("AAPL", price="101.00")
    EngineService(conn, provider).init(symbols=["AAPL"], strategies=["breakout"])
    with pytest.raises(TrdError, match="Unknown symbol"):
        BacktestService(conn).run(symbols=["ZZZT"])
