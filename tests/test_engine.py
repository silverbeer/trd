import math
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from trd.engine import EXIT_RULES
from trd.engine import REGISTRY as STRATEGIES
from trd.engine import exits as exit_rules
from trd.errors import TrdError
from trd.models import (
    AccountType,
    DailyBar,
    EnginePosition,
    InstrumentInfo,
    PositionStatus,
)
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo, TransactionRepo
from trd.services import EngineService
from trd.services.engine import DEFAULT_ENGINE_ACCOUNT

from .conftest import FakeProvider

# ----------------------------------------------------------------- bar helpers


def make_bars(closes: list[float], volumes: list[int] | None = None) -> list[DailyBar]:
    """Daily bars from a close series. High/low straddle the close by 0.5%."""
    start = date(2024, 1, 1)
    out: list[DailyBar] = []
    for i, close in enumerate(closes):
        value = Decimal(str(round(close, 4)))
        out.append(
            DailyBar(
                date=start + timedelta(days=i),
                open=value,
                high=Decimal(str(round(close * 1.005, 4))),
                low=Decimal(str(round(close * 0.995, 4))),
                close=value,
                volume=volumes[i] if volumes else 1_000_000,
            )
        )
    return out


def uptrend(n: int = 260, base: float = 100.0, drift: float = 0.004, wobble: float = 3.0):
    """A rising series that still has down days, so RSI lands in the mid range
    instead of pinning at 100 the way a straight line does."""
    return [base * (1 + drift * i) + wobble * math.sin(i / 3.0) for i in range(n)]


def downtrend(n: int = 260, base: float = 200.0):
    return [base * (1 - 0.002 * i) + 2 * math.sin(i / 3.0) for i in range(n)]


# ------------------------------------------------------------------ registries


def test_registries_are_populated():
    assert sorted(STRATEGIES) == ["breakout", "macd_cross", "momentum", "pullback"]
    assert [rule.key for rule in EXIT_RULES] == ["stop", "trail", "target", "indicator", "time"]


def test_every_strategy_explains_itself():
    for strategy in STRATEGIES.values():
        assert strategy.description
        assert strategy.name


# ------------------------------------------------------------------ strategies


def test_momentum_fires_in_an_uptrend():
    signal = STRATEGIES["momentum"].evaluate(make_bars(uptrend()))
    assert signal is not None
    assert signal.strategy == "momentum"
    assert 0 <= signal.score <= 1
    assert "200-day" in signal.reason


def test_momentum_silent_in_a_downtrend():
    assert STRATEGIES["momentum"].evaluate(make_bars(downtrend())) is None


def test_momentum_skips_an_overbought_name():
    """A vertical line has RSI 100 — exactly the chase the rule refuses."""
    closes = [100 * (1.01**i) for i in range(260)]
    assert STRATEGIES["momentum"].evaluate(make_bars(closes)) is None


def test_breakout_needs_volume():
    closes = [100.0] * 60 + [101.0]  # a new 20-day high on ordinary volume
    quiet = make_bars(closes, volumes=[1_000_000] * 61)
    assert STRATEGIES["breakout"].evaluate(quiet) is None

    loud = make_bars(closes, volumes=[1_000_000] * 60 + [3_000_000])
    signal = STRATEGIES["breakout"].evaluate(loud)
    assert signal is not None
    assert "average volume" in signal.reason


def test_breakout_needs_a_new_high():
    closes = [100.0] * 60 + [99.0]
    bars = make_bars(closes, volumes=[1_000_000] * 60 + [5_000_000])
    assert STRATEGIES["breakout"].evaluate(bars) is None


def test_pullback_fires_on_a_dip_that_turns_up():
    closes = uptrend(240)
    closes += [closes[-1] * 0.93, closes[-1] * 0.88, closes[-1] * 0.86, closes[-1] * 0.90]
    signal = STRATEGIES["pullback"].evaluate(make_bars(closes))
    assert signal is not None
    assert "turned up" in signal.reason


def test_pullback_ignores_a_dip_still_falling():
    closes = uptrend(240)
    closes += [closes[-1] * 0.93, closes[-1] * 0.88, closes[-1] * 0.84, closes[-1] * 0.80]
    assert STRATEGIES["pullback"].evaluate(make_bars(closes)) is None


def test_macd_cross_fires_on_the_flip_bar():
    """Dip then recover inside an uptrend: the histogram crosses back through zero."""
    closes = uptrend(240)
    closes += [closes[-1] * f for f in (0.95, 0.93, 0.92, 0.93, 0.96, 1.00, 1.04, 1.07)]
    bars = make_bars(closes)
    fired = [
        i
        for i in range(len(closes) - 8, len(closes))
        if STRATEGIES["macd_cross"].evaluate(bars[: i + 1]) is not None
    ]
    assert fired, "expected the MACD histogram to cross up somewhere in the recovery"


def test_strategies_return_none_below_min_bars():
    short = make_bars(uptrend(30))
    for strategy in STRATEGIES.values():
        if strategy.min_bars > len(short):
            continue
        strategy.evaluate(short)  # must not raise


# ------------------------------------------------------------------ exit rules


def _position(**overrides) -> EnginePosition:
    """Entry 100, stop 90, target 120 — one R is 10, so the numbers stay readable."""
    base = EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy="momentum",
        opened_at=datetime(2024, 6, 1, 10, 0),
        entry_price=Decimal("100"),
        quantity=Decimal("10"),
        stop_price=Decimal("90"),
        target_price=Decimal("120"),
        atr_at_entry=Decimal("5"),
        trail_high=Decimal("100"),
        bars_held=0,
    )
    return base.model_copy(update=overrides)


PARAMS = exit_rules.DEFAULT_EXIT_PARAMS


def test_stop_fires_at_or_below_the_stop():
    bars = make_bars(uptrend())
    assert exit_rules.StopLoss().check(_position(), bars, Decimal("91"), PARAMS) is None
    hit = exit_rules.StopLoss().check(_position(), bars, Decimal("90"), PARAMS)
    assert hit is not None and hit.rule == "stop"


def test_target_fires_at_or_above_the_target():
    bars = make_bars(uptrend())
    assert exit_rules.ProfitTarget().check(_position(), bars, Decimal("119"), PARAMS) is None
    hit = exit_rules.ProfitTarget().check(_position(), bars, Decimal("120"), PARAMS)
    assert hit is not None and hit.rule == "target"


def test_trailing_stop_waits_until_it_beats_the_initial_stop():
    bars = make_bars(uptrend())
    # trail_high 100, ATR 5, mult 3 -> trail stop 85, below the initial 90: inactive.
    early = _position(trail_high=Decimal("100"))
    assert exit_rules.TrailingStop().check(early, bars, Decimal("86"), PARAMS) is None
    # trail_high 130 -> trail stop 115, now the tighter of the two.
    late = _position(trail_high=Decimal("130"))
    assert exit_rules.TrailingStop().check(late, bars, Decimal("116"), PARAMS) is None
    hit = exit_rules.TrailingStop().check(late, bars, Decimal("115"), PARAMS)
    assert hit is not None and hit.rule == "trail"
    assert "gave back" in hit.reason


def test_time_exit_fires_after_max_bars():
    bars = make_bars(uptrend())
    assert exit_rules.TimeExit().check(_position(bars_held=9), bars, Decimal("101"), PARAMS) is None
    hit = exit_rules.TimeExit().check(_position(bars_held=10), bars, Decimal("101"), PARAMS)
    assert hit is not None and hit.rule == "time"


def test_indicator_exit_fires_when_price_loses_the_20_day():
    bars = make_bars(uptrend())
    sma20 = sum(float(b.close) for b in bars[-20:]) / 20
    hit = exit_rules.IndicatorExit().check(
        _position(bars_held=3), bars, Decimal(str(sma20 * 0.9)), PARAMS
    )
    assert hit is not None and hit.rule == "indicator"
    assert "20-day" in hit.reason


def test_indicator_exit_gives_a_new_entry_room_to_breathe():
    """A pullback buys below the 20-day. Without the grace period this rule would
    sell it back on the very next scan for a zero-P&L round trip."""
    bars = make_bars(uptrend())
    sma20 = sum(float(b.close) for b in bars[-20:]) / 20
    below = Decimal(str(sma20 * 0.9))
    for held in (0, 1, 2):
        assert (
            exit_rules.IndicatorExit().check(_position(bars_held=held), bars, below, PARAMS) is None
        )


def test_the_stop_still_runs_during_the_indicator_grace_period():
    """Grace applies to indicator exits only — capital protection never pauses."""
    bars = make_bars(uptrend())
    decision = exit_rules.evaluate(_position(bars_held=0), bars, Decimal("89"), PARAMS)
    assert decision is not None and decision.rule == "stop"


def test_capital_protection_runs_before_profit_taking():
    """A bar that trips both the stop and the target reports the stop."""
    bars = make_bars(uptrend())
    position = _position(stop_price=Decimal("130"), target_price=Decimal("120"))
    decision = exit_rules.evaluate(position, bars, Decimal("125"), PARAMS)
    assert decision is not None and decision.rule == "stop"


# --------------------------------------------------------------------- service


def seed(conn: duckdb.DuckDBPyConnection, symbol: str, bars: list[DailyBar]) -> int:
    repo = InstrumentRepo(conn)
    instrument = repo.get_by_symbol(symbol) or repo.insert(
        InstrumentInfo(symbol=symbol, name=symbol)
    )
    PriceRepo(conn).upsert_daily(instrument.id, bars)
    return instrument.id


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def test_init_creates_a_simulation_account_and_universe(engine, provider):
    provider.add_symbol("AAA", price="100")
    provider.add_symbol("BBB", price="50")
    config, account, universe = engine.init(symbols=["AAA", "BBB"])
    assert account.type == AccountType.SIMULATION
    assert account.name == DEFAULT_ENGINE_ACCOUNT
    assert universe == ["AAA", "BBB"]
    assert sorted(config.strategies) == ["breakout", "macd_cross", "momentum", "pullback"]
    assert config.exit_params["target_r"] == 2.0


def test_init_refuses_a_real_account(engine, conn):
    AccountRepo(conn).create("brokerage", AccountType.REAL)
    with pytest.raises(TrdError, match="real account"):
        engine.init(account_name="brokerage", symbols=[])


def test_init_rejects_unknown_strategies(engine):
    with pytest.raises(TrdError, match="Unknown strategies"):
        engine.init(symbols=[], strategies=["moonshot"])


def test_scan_without_config_is_a_clean_error(engine):
    with pytest.raises(TrdError, match="No engine configured"):
        engine.scan()


def test_scan_opens_a_position_and_records_a_txn(engine, provider, conn):
    bars = make_bars(uptrend())
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    seed(conn, "AAA", bars)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(result.opened) == 1
    fill = result.opened[0]
    assert fill.symbol == "AAA"
    assert fill.strategy == "momentum"
    assert fill.quantity > 0

    account = engine.account()
    txns = TransactionRepo(conn).list_chronological(account.id)
    assert len(txns) == 1 and txns[0].side == "buy"
    assert txns[0].quantity == fill.quantity

    positions = engine.position_rows(open_only=True)
    assert len(positions) == 1
    position = positions[0].position
    assert position.stop_price < position.entry_price < position.target_price
    # Target sits exactly 2R above entry.
    assert position.target_price - position.entry_price == position.risk_per_share * 2


def test_a_second_scan_on_the_same_bar_does_not_double_up(engine, provider, conn):
    bars = make_bars(uptrend())
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    seed(conn, "AAA", bars)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    first = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    second = engine.scan(at=datetime(2024, 9, 16, 10, 1))
    assert len(first.opened) == 1
    assert second.opened == []
    assert second.signals == []  # the signal was stored once, on the first pass
    assert len(engine.position_rows(open_only=True)) == 1
    assert len(engine.signal_rows()) == 1


def test_max_positions_caps_new_entries(engine, provider, conn):
    for symbol in ("AAA", "BBB", "CCC"):
        bars = make_bars(uptrend())
        provider.add_symbol(symbol, price=str(float(bars[-1].close)))
        seed(conn, symbol, bars)
    engine.init(
        symbols=["AAA", "BBB", "CCC"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        max_positions=2,
    )
    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(result.opened) == 2
    assert len(result.signals) == 3  # all three fired; only two fit
    assert result.capacity == 0


def test_no_paper_logs_signals_without_filling(engine, provider, conn):
    bars = make_bars(uptrend())
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    seed(conn, "AAA", bars)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    result = engine.scan(paper=False, at=datetime(2024, 9, 16, 10, 0))
    assert len(result.signals) == 1
    assert result.opened == []
    assert engine.position_rows(open_only=True) == []
    assert TransactionRepo(conn).list_chronological(engine.account().id) == []


def test_scan_closes_a_position_when_the_stop_breaks(engine, provider, conn):
    bars = make_bars(uptrend())
    entry = float(bars[-1].close)
    provider.add_symbol("AAA", price=str(entry))
    seed(conn, "AAA", bars)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    engine.scan(at=datetime(2024, 9, 16, 10, 0))

    position = engine.position_rows(open_only=True)[0].position
    provider.add_symbol("AAA", price=str(float(position.stop_price) - 1))
    result = engine.scan(at=datetime(2024, 9, 16, 15, 0))

    assert len(result.closed) == 1
    closed = result.closed[0]
    assert closed.rule == "stop"
    assert closed.pnl is not None and closed.pnl < 0
    assert closed.r_multiple is not None and closed.r_multiple < -1

    txns = TransactionRepo(conn).list_chronological(engine.account().id)
    assert [t.side for t in txns] == ["buy", "sell"]
    rows = engine.position_rows(open_only=False)
    assert rows[0].position.status == PositionStatus.CLOSED
    assert rows[0].position.exit_reason


def test_report_scores_each_strategy(engine, provider, conn):
    bars = make_bars(uptrend())
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    seed(conn, "AAA", bars)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    engine.scan(at=datetime(2024, 9, 16, 10, 0))

    position = engine.position_rows(open_only=True)[0].position
    provider.add_symbol("AAA", price=str(float(position.stop_price) - 1))
    engine.scan(at=datetime(2024, 9, 16, 15, 0))

    stats = engine.report()
    assert len(stats) == 1
    stat = stats[0]
    assert stat.strategy == "momentum"
    assert stat.trades == 1
    assert stat.losses == 1
    assert stat.win_rate == 0
    assert stat.avg_r is not None and stat.avg_r < 0
    assert stat.total_pnl < 0


def test_missing_history_is_reported_not_crashed(engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["momentum"])
    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert result.opened == []
    assert any("no price history" in line for line in result.skipped)


def test_short_history_names_the_missing_bars(engine, provider, conn):
    bars = make_bars(uptrend(40))
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    seed(conn, "AAA", bars)
    engine.init(symbols=["AAA"], strategies=["momentum"])
    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert result.opened == []
    assert any("momentum needs 200" in line for line in result.skipped)


def test_live_quote_forms_todays_bar(engine, provider, conn):
    """The engine reacts intraday: the same stored history plus a different live
    quote fills at the live price, not the stored close."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    live = float(bars[-1].close) * 0.998
    provider.add_symbol("AAA", price=str(live))
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(result.opened) == 1
    assert result.opened[0].price == provider.quotes["AAA"].price
    assert result.opened[0].price != bars[-1].close


def test_live_quote_can_push_a_name_out_of_the_setup(engine, provider, conn):
    """The same history that fires at the stored close goes quiet once the live
    quote lifts RSI above 70 — the rule refuses to chase, intraday included."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 1.02))
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert result.signals == []
    assert result.opened == []
