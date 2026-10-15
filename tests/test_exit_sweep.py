"""The exit sweep's instruments (SB-1033): per-trade excursion and pooled capture
on a backtest, the two new knobs (trail activation, opening entry delay), and
`--param` overrides that vary one axis against the same history."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from datetime import time as dtime
from decimal import Decimal

import duckdb
import pytest

from trd.engine import DEFAULT_EXIT_PARAMS
from trd.engine.exits import TrailingStop
from trd.errors import TrdError
from trd.models import EnginePosition
from trd.repos import InstrumentRepo, PriceRepo
from trd.services import BacktestService, EngineService
from trd.services.engine import entry_opens_at
from trd.services.outcomes import pooled_capture
from trd.timeframes import SESSION_OPEN_MINUTE

from .conftest import FakeProvider
from .test_backtest import (
    _day_params,
    _intraday_series,
    bar,
    breakout_series,
    entry_levels,
    run,
)

# ------------------------------------------------------- excursion + capture


def test_a_trade_that_ran_before_it_stopped_records_what_it_was_offered():
    """+1.5R on the way up, then the stop: offered 1.5R, booked -1R. The number
    the sweep is scored on, so it must come from the bars the trade lived through."""
    series = breakout_series()
    entry, stop, _target = entry_levels(series)
    risk = entry - stop
    peak = entry + risk * Decimal("1.5")
    d1 = series[-1].date + timedelta(days=1)
    d2 = d1 + timedelta(days=1)
    series = [
        *series,
        bar(d1, float(entry), float(peak), float(entry) - 0.1, float(entry) + 0.2),
        bar(d2, float(stop) + 0.1, float(stop) + 0.2, float(stop) - 0.5, float(stop) - 0.2),
    ]

    result = run({"AAA": series})
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.rule == "stop"
    # The bar helper goes through float, so compare at a sane precision.
    assert trade.mfe_r is not None and round(trade.mfe_r, 6) == Decimal("1.5")
    assert trade.r_multiple == Decimal(-1)
    # Pooled: -1R booked out of 1.5R offered.
    assert result.capture is not None and round(result.capture, 6) == round(
        Decimal(-1) / Decimal("1.5"), 6
    )


def test_an_intrabar_exit_bar_counts_only_up_to_the_exit_price():
    """The target fills at its level inside the bar; the wick above it happened
    after the trade left and is not opportunity it was offered."""
    series = breakout_series()
    _entry, _stop, target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 102.0, float(target) + 5, 101.5, 103.0)]

    result = run({"AAA": series})
    trade = result.trades[0]
    assert trade.rule == "target"
    assert trade.mfe_r == Decimal(2)
    assert result.capture == Decimal(1)


def test_result_capture_is_the_shared_pooled_definition():
    result = run(
        {"AAA": _intraday_series()},
        strategies=["momentum"],
        exit_params=_day_params(),
        timeframe="5m",
    )
    assert result.trades
    assert all(t.mfe_r is not None for t in result.trades)
    assert result.capture == pooled_capture((t.mfe_r, t.r_multiple) for t in result.trades)
    assert "capture" in json.loads(result.model_dump_json())


def test_pooled_capture_ignores_trades_offered_nothing():
    assert pooled_capture([(Decimal(0), Decimal(-1)), (Decimal(2), Decimal(1))]) == Decimal("0.5")
    assert pooled_capture([(Decimal(0), Decimal(-1))]) is None


# ---------------------------------------------------------- trail activation


def _position(trail_high: str) -> EnginePosition:
    # entry 100, stop 98 (1R = 2), ATR 1: the 3xATR trail sits at trail_high - 3.
    return EnginePosition(
        id=1,
        account_id=0,
        instrument_id=1,
        strategy="breakout",
        opened_at=datetime(2026, 9, 1, 10, 0),
        entry_price=Decimal(100),
        quantity=Decimal(10),
        stop_price=Decimal(98),
        target_price=Decimal(104),
        atr_at_entry=Decimal(1),
        trail_high=Decimal(trail_high),
    )


def _trail(position: EnginePosition, price: str, **params: float):
    return TrailingStop().check(
        position,
        [],
        [],
        Decimal(price),
        dict(DEFAULT_EXIT_PARAMS, **params),
        datetime(2026, 9, 1, 11, 0),
        "5m",
    )


def test_trail_with_activation_off_behaves_as_it_always_has():
    # Peak 102 (+1R): trail at 99, above the 98 stop, and price 98.9 is under it.
    assert _trail(_position("102"), "98.9") is not None


def test_trail_waits_until_the_trade_has_run_the_activation_r():
    assert _trail(_position("102"), "98.9", trail_activate_r=1.5) is None
    # Peak 103 = +1.5R: activated, trail at 100.
    decision = _trail(_position("103"), "99.9", trail_activate_r=1.5)
    assert decision is not None and decision.level == Decimal(100)


# ------------------------------------------------------- opening entry delay


def test_entry_opens_at_is_off_by_default():
    assert entry_opens_at(dict(DEFAULT_EXIT_PARAMS)) is None
    assert entry_opens_at({"entry_delay_minutes": 10}) == SESSION_OPEN_MINUTE + 10


def test_the_opening_delay_keeps_entries_out_of_the_first_minutes():
    """Discriminates: without the delay this series does enter before 09:40."""
    baseline = run(
        {"AAA": _intraday_series()},
        strategies=["momentum"],
        exit_params=_day_params(),
        timeframe="5m",
    )
    assert any(t.entry_at.time() < dtime(9, 40) for t in baseline.trades)

    delayed = run(
        {"AAA": _intraday_series()},
        strategies=["momentum"],
        exit_params=_day_params(entry_delay_minutes=10.0),
        timeframe="5m",
    )
    assert delayed.trades
    assert all(t.entry_at.time() >= dtime(9, 40) for t in delayed.trades)


# --------------------------------------------------------- service overrides


def _configured(conn: duckdb.DuckDBPyConnection) -> None:
    provider = FakeProvider()
    provider.add_symbol("AAPL", price="101.00")
    EngineService(conn, provider).init(symbols=["AAPL"], strategies=["breakout"])
    instrument = InstrumentRepo(conn).get_by_symbol("AAPL")
    assert instrument is not None
    series = breakout_series()
    _entry, _stop, target = entry_levels(series)
    next_day = series[-1].date + timedelta(days=1)
    series = [*series, bar(next_day, 102.0, float(target) + 1, 101.5, 103.0)]
    PriceRepo(conn).upsert_daily(instrument.id, series)


def test_a_param_override_changes_the_replay_and_is_recorded(conn: duckdb.DuckDBPyConnection):
    _configured(conn)
    base = BacktestService(conn).run()
    assert base.trades[0].r_multiple == Decimal(2)
    assert base.overrides == {}

    swept = BacktestService(conn).run(params_override={"target_r": 1})
    assert swept.trades[0].rule == "target"
    assert swept.trades[0].r_multiple == Decimal(1)
    assert swept.overrides == {"target_r": 1.0}


def test_an_unknown_param_is_refused_rather_than_silently_ignored(
    conn: duckdb.DuckDBPyConnection,
):
    _configured(conn)
    with pytest.raises(TrdError, match=r"Unknown exit param.*trail_mult"):
        BacktestService(conn).run(params_override={"trail_mult": 2})


def test_a_negative_param_is_refused(conn: duckdb.DuckDBPyConnection):
    _configured(conn)
    with pytest.raises(TrdError, match="negative"):
        BacktestService(conn).run(params_override={"stop_atr_mult": -1})


def test_cli_param_flag_reaches_the_replay(cli_env, tmp_path):
    from typer.testing import CliRunner

    from trd.cli.app import app
    from trd.db.connection import connect

    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    conn = connect(home / "trd.duckdb")
    _configured(conn)
    conn.close()

    result = CliRunner().invoke(app, ["engine", "backtest", "-p", "target_r=1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["overrides"] == {"target_r": 1.0}
    assert Decimal(payload["trades"][0]["r_multiple"]) == Decimal(1)


@pytest.mark.parametrize("delay, opens", [(0.0, True), (45.0, False)])
def test_the_live_scan_honours_the_opening_delay(conn, provider, delay, opens):
    """The same gate the backtest measures. 10:00 is inside a 45-minute delay
    (entries open 10:15); with no delay the identical scan takes the trade."""
    from tests.test_engine import make_intraday_bars, seed_intraday
    from tests.test_engine_intraday import _day_closes

    engine = EngineService(conn, provider)
    bars = make_intraday_bars(_day_closes())
    seed_intraday(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"flat_at_minute": 1555.0, "entry_delay_minutes": delay},
        timeframe="5m",
    )

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert bool(result.opened) is opens
    assert any("first 45m of the session" in s for s in result.skipped) is not opens
