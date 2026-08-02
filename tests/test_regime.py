"""The market-regime gate.

Every entry rule asks whether one *name* is in an uptrend. None asks what the
market is doing, so in a broad selloff the engine fills all five slots with
whatever is still above its own 200-day. This gate blocks new entries when the
tape is broken, and blocks nothing else — an open position keeps running its
exits, because a regime that stops you buying is not a reason to abandon a stop
that is already working.

Off by default. `test_off_by_default_changes_nothing` is the one that guarantees
existing engines are untouched.
"""

from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.engine import regime
from trd.errors import TrdError
from trd.models import DailyBar
from trd.services import EngineService
from trd.services.backtest import simulate


def _flat(n: int, value: float) -> list[DailyBar]:
    """A dead-flat series, so an SMA equals the close and only the last bar moves it."""
    start = date(2026, 1, 1)
    return [
        DailyBar(
            date=start + timedelta(days=i),
            open=Decimal(str(value)),
            high=Decimal(str(value)),
            low=Decimal(str(value)),
            close=Decimal(str(value)),
            volume=1_000,
        )
        for i in range(n)
    ]


def _with_last(bars: list[DailyBar], close: float) -> list[DailyBar]:
    return [*bars[:-1], bars[-1].model_copy(update={"close": Decimal(str(close))})]


ON = {"regime_sma": 50.0}


# ------------------------------------------------------------------ the gate


def test_off_by_default_changes_nothing() -> None:
    """The guarantee for every engine that predates this."""
    assert regime.is_configured({}) is False
    assert regime.blocks_entries({}, trend_bars=_flat(60, 100.0)) is None


def test_below_the_average_blocks(spy=None) -> None:
    bars = _with_last(_flat(60, 100.0), 90.0)
    reason = regime.blocks_entries(ON, trend_bars=bars)
    assert reason is not None
    assert "SPY" in reason and "below its 50-day" in reason


def test_above_the_average_allows() -> None:
    bars = _with_last(_flat(60, 100.0), 110.0)
    assert regime.blocks_entries(ON, trend_bars=bars) is None


def test_vix_above_the_ceiling_blocks() -> None:
    reason = regime.blocks_entries(
        {"regime_vix_max": 30.0}, vix_bars=_with_last(_flat(5, 15.0), 42.0)
    )
    assert reason is not None
    assert "^VIX" in reason and "42" in reason


def test_vix_below_the_ceiling_allows() -> None:
    assert regime.blocks_entries({"regime_vix_max": 30.0}, vix_bars=_flat(5, 15.0)) is None


def test_either_switch_can_block_alone() -> None:
    """They are independent gates, not a combined score."""
    calm_but_falling = regime.blocks_entries(
        {**ON, "regime_vix_max": 30.0},
        trend_bars=_with_last(_flat(60, 100.0), 90.0),
        vix_bars=_flat(5, 12.0),
    )
    assert calm_but_falling is not None and "below its 50-day" in calm_but_falling


def test_missing_data_does_not_block() -> None:
    """A gate that halts trading because a sync failed looks exactly like a gate
    that is working, and the engine would sit flat with nothing explaining it."""
    assert regime.blocks_entries(ON, trend_bars=None) is None
    assert regime.blocks_entries(ON, trend_bars=[]) is None


def test_too_few_bars_for_the_average_does_not_block() -> None:
    assert regime.blocks_entries({"regime_sma": 200.0}, trend_bars=_flat(10, 100.0)) is None


def test_slice_to_is_inclusive_and_ordered() -> None:
    bars = _flat(10, 100.0)
    cut = bars[4].date
    sliced = regime.slice_to(bars, cut)
    assert len(sliced) == 5
    assert sliced[-1].date == cut


# ---------------------------------------------------------- the live engine


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _seed_regime(conn, provider, spy_closes: list[float], vix: float | None = None) -> None:
    provider.add_symbol("SPY", price=str(spy_closes[-1]))
    seed(conn, "SPY", make_bars(spy_closes))
    if vix is not None:
        provider.add_symbol("^VIX", price=str(vix))
        seed(conn, "^VIX", make_bars([vix] * 30))


def _tradeable(engine, conn, provider) -> None:
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)


def test_a_broken_tape_takes_no_entry(engine, conn, provider) -> None:
    """The behaviour the whole ticket is about."""
    _tradeable(engine, conn, provider)
    _seed_regime(conn, provider, [200.0] * 59 + [150.0])  # SPY well under its average
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"regime_sma": 50.0},
    )

    result = engine.scan()

    assert result.opened == []
    assert any("no new entries while the whole tape" in line for line in result.skipped)


def test_a_healthy_tape_still_trades(engine, conn, provider) -> None:
    """The gate must not become a reason a working engine stops trading."""
    _tradeable(engine, conn, provider)
    _seed_regime(conn, provider, [float(100 + i) for i in range(60)])  # SPY rising
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"regime_sma": 50.0},
    )

    assert len(engine.scan().opened) == 1


def test_no_regime_bars_does_not_stop_trading(engine, conn, provider) -> None:
    _tradeable(engine, conn, provider)
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"regime_sma": 50.0},
    )
    assert len(engine.scan().opened) == 1


def test_exits_still_run_while_entries_are_blocked(engine, conn, provider) -> None:
    """A regime that stops you buying is not a reason to abandon a working stop."""
    _tradeable(engine, conn, provider)
    _seed_regime(conn, provider, [float(100 + i) for i in range(60)])
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"regime_sma": 50.0},
    )
    assert len(engine.scan().opened) == 1
    position = engine.position_rows(open_only=True)[0].position

    # Tape breaks *and* the held name drops through its stop, on the same pass.
    seed(conn, "SPY", make_bars([200.0] * 59 + [150.0]))
    provider.add_symbol("AAA", price=str(float(position.stop_price) * 0.99), volume=1_200_000)

    result = engine.scan()

    assert len(result.closed) == 1
    assert result.closed[0].rule == "stop"


def test_engine_init_registers_the_regime_instruments(engine, provider) -> None:
    """They become tracked instruments so `trd sync` pulls their bars — but they
    are never added to the watchlist, because the engine reads them, never trades
    them."""
    provider.add_symbol("AAA", price="100")
    provider.add_symbol("SPY", price="500")
    provider.add_symbol("^VIX", price="15")
    engine.init(symbols=["AAA"])

    engine.ensure_regime_instruments()

    assert engine.instruments.get_by_symbol("SPY") is not None
    assert engine.instruments.get_by_symbol("^VIX") is not None
    assert [i.symbol for i in engine.universe()] == ["AAA"]


# -------------------------------------------------------------- the backtest


def _sim(**kw):
    """A run that genuinely trades when ungated — otherwise a test asserting the
    gate blocked everything would pass even with the gate doing nothing."""
    from tests.test_backtest import breakout_series
    from trd.engine.exits import DEFAULT_EXIT_PARAMS

    series = {"AAA": breakout_series()}
    params = {**DEFAULT_EXIT_PARAMS, **kw.pop("exit_params", {})}
    return simulate(
        series,
        strategies=["breakout"],
        position_size=Decimal("1000"),
        max_positions=2,
        exit_params=params,
        **kw,
    )


def _activity(result) -> int:
    """Entries taken, whether or not they closed inside the window."""
    return len(result.trades) + result.open_at_end


def test_backtest_without_the_gate_trades() -> None:
    """The control. If this ever stops trading, the blocking test below becomes
    vacuous and must be re-fixtured."""
    result = _sim()
    assert _activity(result) > 0
    assert result.regime_blocked == 0


def test_backtest_gate_blocks_and_is_counted() -> None:
    """Same series, same rules, tape permanently broken: nothing is taken."""
    spy = make_bars([200.0] * 59 + [150.0] * 300)
    result = _sim(exit_params={"regime_sma": 50.0}, regime_bars={"SPY": spy})
    assert _activity(result) == 0
    assert result.regime_blocked > 0


def test_the_gate_reads_only_the_prefix_it_is_given() -> None:
    """No lookahead. The same series answers differently depending on where you
    stand in it — which is exactly what `slice_to` buys the backtest, and why a
    later recovery cannot retroactively unblock an earlier bar."""
    broken_then_recovered = make_bars([200.0] * 59 + [150.0] * 5 + [400.0] * 20)
    during_the_break = broken_then_recovered[63].date

    assert (
        regime.blocks_entries(
            ON, trend_bars=regime.slice_to(broken_then_recovered, during_the_break)
        )
        is not None
    )
    assert regime.blocks_entries(ON, trend_bars=broken_then_recovered) is None


def test_backtest_refuses_when_the_gate_has_no_bars(
    conn: duckdb.DuckDBPyConnection, provider
) -> None:
    """Silently running ungated would answer a different question than the one
    asked, and the caller would never know."""
    from trd.services.backtest import BacktestService

    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price="100")
    EngineService(conn, provider).init(
        symbols=["AAA"], strategies=["breakout"], exit_params={"regime_sma": 50.0}
    )

    with pytest.raises(TrdError, match="regime filter needs"):
        BacktestService(conn).run(years=1)
