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
    EarningsDate,
    EnginePosition,
    InstrumentInfo,
    PositionStatus,
)
from trd.repos import AccountRepo, EarningsRepo, InstrumentRepo, PriceRepo, TransactionRepo
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
    assert [rule.key for rule in EXIT_RULES] == [
        "stop",
        "trail",
        "target",
        "indicator",
        "time",
        "session_close",
    ]


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
# Any mid-session moment: the clock only matters to session_close, which is off
# unless flat_at_minute is set.
MIDDAY = datetime(2024, 9, 16, 12, 0)


def test_stop_fires_at_or_below_the_stop():
    bars = make_bars(uptrend())
    assert exit_rules.StopLoss().check(_position(), bars, Decimal("91"), PARAMS, MIDDAY) is None
    hit = exit_rules.StopLoss().check(_position(), bars, Decimal("90"), PARAMS, MIDDAY)
    assert hit is not None and hit.rule == "stop"


def test_target_fires_at_or_above_the_target():
    bars = make_bars(uptrend())
    assert (
        exit_rules.ProfitTarget().check(_position(), bars, Decimal("119"), PARAMS, MIDDAY) is None
    )
    hit = exit_rules.ProfitTarget().check(_position(), bars, Decimal("120"), PARAMS, MIDDAY)
    assert hit is not None and hit.rule == "target"


def test_trailing_stop_waits_until_it_beats_the_initial_stop():
    bars = make_bars(uptrend())
    # trail_high 100, ATR 5, mult 3 -> trail stop 85, below the initial 90: inactive.
    early = _position(trail_high=Decimal("100"))
    assert exit_rules.TrailingStop().check(early, bars, Decimal("86"), PARAMS, MIDDAY) is None
    # trail_high 130 -> trail stop 115, now the tighter of the two.
    late = _position(trail_high=Decimal("130"))
    assert exit_rules.TrailingStop().check(late, bars, Decimal("116"), PARAMS, MIDDAY) is None
    hit = exit_rules.TrailingStop().check(late, bars, Decimal("115"), PARAMS, MIDDAY)
    assert hit is not None and hit.rule == "trail"
    assert "gave back" in hit.reason


def test_time_exit_fires_after_max_bars():
    bars = make_bars(uptrend())
    assert (
        exit_rules.TimeExit().check(_position(bars_held=9), bars, Decimal("101"), PARAMS, MIDDAY)
        is None
    )
    hit = exit_rules.TimeExit().check(_position(bars_held=10), bars, Decimal("101"), PARAMS, MIDDAY)
    assert hit is not None and hit.rule == "time"


def test_indicator_exit_fires_when_price_loses_the_20_day():
    bars = make_bars(uptrend())
    sma20 = sum(float(b.close) for b in bars[-20:]) / 20
    hit = exit_rules.IndicatorExit().check(
        _position(bars_held=3), bars, Decimal(str(sma20 * 0.9)), PARAMS, MIDDAY
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
            exit_rules.IndicatorExit().check(_position(bars_held=held), bars, below, PARAMS, MIDDAY)
            is None
        )


def test_the_stop_still_runs_during_the_indicator_grace_period():
    """Grace applies to indicator exits only — capital protection never pauses."""
    bars = make_bars(uptrend())
    decision = exit_rules.evaluate(_position(bars_held=0), bars, Decimal("89"), PARAMS, MIDDAY)
    assert decision is not None and decision.rule == "stop"


def test_capital_protection_runs_before_profit_taking():
    """A bar that trips both the stop and the target reports the stop."""
    bars = make_bars(uptrend())
    position = _position(stop_price=Decimal("130"), target_price=Decimal("120"))
    decision = exit_rules.evaluate(position, bars, Decimal("125"), PARAMS, MIDDAY)
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
    # A live price of its own, so the run reaches the bar-count check instead of
    # stopping at the stale-quote guard — this series ends long before `at`.
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 1.01))
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


# ------------------------------------------------------------- stale quotes


def test_a_quote_that_is_only_yesterdays_close_takes_no_entry(engine, provider, conn):
    """A name that has not printed yet still answers a quote request — with the
    prior close. Trading that builds the whole position on a price that never
    existed, so the pass skips the symbol and says why."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    # One day past the last stored bar: today's bar would be synthetic.
    result = engine.scan(at=datetime(2024, 9, 17, 9, 30))

    assert result.opened == []
    assert result.signals == []  # the signal itself would be an artifact
    assert any("no trade print yet today" in line for line in result.skipped)


def test_a_missing_quote_is_not_treated_as_a_flat_day(engine, provider, conn):
    """No quote at all is the same fiction as a stale one: filling at the stored
    close prices a trade off a bar that has not happened."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    provider.drop_quote("AAA")

    result = engine.scan(at=datetime(2024, 9, 17, 9, 30))

    assert result.opened == []
    assert any("no trade print yet today" in line for line in result.skipped)


def test_a_real_bar_for_today_lets_a_matching_quote_through(engine, provider, conn):
    """Once today's bar is real, a quote equal to its close is a flat tape, not a
    stale feed — the guard must not swallow it."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close)))
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    # bars[-1].date IS 2024-09-16, so the stored series already covers today.
    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert len(result.opened) == 1


# ---------------------------------------------------------- fractional sizing


def test_position_size_is_filled_exactly_with_fractional_shares(engine, provider, conn):
    """Flooring to whole shares un-fixes fixed-dollar sizing. $1000 into a $340
    name is 2.94 shares, not 2 — otherwise the slot is silently 68% funded."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    live = Decimal(str(round(float(bars[-1].close) * 0.998, 4)))
    provider.add_symbol("AAA", price=str(live))
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("1000"))

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert len(result.opened) == 1
    quantity = result.opened[0].quantity
    assert quantity != quantity.to_integral_value()  # genuinely fractional
    # The slot is funded to the cent, not to the nearest whole share.
    assert abs(quantity * live - Decimal("1000")) < Decimal("0.01")
    assert quantity.as_tuple().exponent >= -6  # 6dp, inside DECIMAL(24, 8)


def test_a_share_priced_above_the_slot_is_still_tradeable(engine, provider, conn):
    """A share priced above the whole slot — SNDK at $1278 against $1000 — used to
    be dropped from the universe without ever surfacing as a signal. It buys a
    fraction of a share now."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    live = Decimal(str(round(float(bars[-1].close) * 0.998, 4)))
    provider.add_symbol("AAA", price=str(live))
    # A slot smaller than one share, the same shape as $1000 against SNDK.
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=live / 2)

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert len(result.opened) == 1
    assert result.opened[0].quantity < 1
    assert result.skipped == []


# --------------------------------------------------------- earnings blackout


def seed_earnings(conn: duckdb.DuckDBPyConnection, instrument_id: int, when: date) -> None:
    EarningsRepo(conn).upsert(instrument_id, [EarningsDate(date=when, eps_estimate=None)])


def test_a_print_inside_the_blackout_holds_the_signal_back(engine, provider, conn):
    """The signal is real — good data, rule genuinely fired — so it is still
    recorded. What the blackout withholds is the entry, because a gap jumps the
    stop and the trade would not risk the 1R its R-multiple claims."""
    bars = make_bars(uptrend())
    instrument_id = seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    seed_earnings(conn, instrument_id, date(2024, 9, 18))  # 2 days out
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert result.opened == []
    assert len(result.signals) == 1  # fired and logged, just not acted on
    assert any("earnings in 2d" in line for line in result.skipped)
    assert engine.position_rows(open_only=True) == []


def test_a_print_outside_the_blackout_trades_normally(engine, provider, conn):
    bars = make_bars(uptrend())
    instrument_id = seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    seed_earnings(conn, instrument_id, date(2024, 9, 30))  # 14 days out
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert len(result.opened) == 1


def test_the_day_after_a_print_is_tradeable_again(engine, provider, conn):
    """Deliberate: the gap-and-volume day is exactly what breakout exists to
    catch. The blackout removes the coin flip, not the setup it creates."""
    bars = make_bars(uptrend())
    instrument_id = seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    seed_earnings(conn, instrument_id, date(2024, 9, 15))  # reported yesterday
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert len(result.opened) == 1


def test_a_zero_blackout_disables_the_guard(engine, provider, conn):
    bars = make_bars(uptrend())
    instrument_id = seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    seed_earnings(conn, instrument_id, date(2024, 9, 16))  # reporting today
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        earnings_blackout_days=0,
    )

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert len(result.opened) == 1


def test_a_name_with_no_known_earnings_date_is_unaffected(engine, provider, conn):
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))

    assert len(result.opened) == 1


def test_blackout_days_cannot_be_negative(engine):
    with pytest.raises(TrdError, match="cannot be negative"):
        engine.init(symbols=[], earnings_blackout_days=-1)


# ------------------------------------------------------------ session close


DAY_PARAMS = {**exit_rules.DEFAULT_EXIT_PARAMS, "flat_at_minute": 1555.0}


def test_session_close_is_off_by_default():
    """A swing engine is built to carry overnight — the rule must stay dormant
    unless a day-mode engine switches it on."""
    bars = make_bars(uptrend())
    late = datetime(2024, 9, 16, 15, 59)
    assert exit_rules.SessionClose().check(_position(), bars, Decimal("101"), PARAMS, late) is None


def test_session_close_fires_at_the_flat_time():
    bars = make_bars(uptrend())
    rule = exit_rules.SessionClose()
    before = datetime(2024, 9, 16, 15, 54)
    assert rule.check(_position(), bars, Decimal("101"), DAY_PARAMS, before) is None
    hit = rule.check(_position(), bars, Decimal("101"), DAY_PARAMS, datetime(2024, 9, 16, 15, 55))
    assert hit is not None and hit.rule == "session_close"
    assert "15:55" in hit.reason


def test_the_stop_still_wins_at_the_bell():
    """Both would fire at 15:55. The stop is the truer reason, and the closed
    trade's R-multiple should be attributed to the rule that actually decided it."""
    bars = make_bars(uptrend())
    decision = exit_rules.evaluate(
        _position(), bars, Decimal("80"), DAY_PARAMS, datetime(2024, 9, 16, 15, 55)
    )
    assert decision is not None and decision.rule == "stop"


def test_no_new_entries_inside_the_cutoff(engine, provider, conn):
    """Entering at 15:50 only to be flattened at 15:55 pays the spread twice for
    five minutes of exposure."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"flat_at_minute": 1555.0},
    )

    late = engine.scan(at=datetime(2024, 9, 16, 15, 30))  # cutoff is 15:25
    assert late.opened == []
    assert any("session close" in line for line in late.skipped)


def test_entries_still_run_before_the_cutoff(engine, provider, conn):
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"flat_at_minute": 1555.0},
    )

    result = engine.scan(at=datetime(2024, 9, 16, 15, 24))
    assert len(result.opened) == 1


def test_a_day_engine_closes_its_position_at_the_bell(engine, provider, conn):
    """End to end: open mid-session, flat by the bell, recorded as a sell."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998))
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"flat_at_minute": 1555.0},
    )

    opened = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(opened.opened) == 1

    closed = engine.scan(at=datetime(2024, 9, 16, 15, 56))
    assert len(closed.closed) == 1
    assert closed.closed[0].rule == "session_close"
    assert engine.position_rows(open_only=True) == []
    assert [t.side for t in TransactionRepo(conn).list_chronological(engine.account().id)] == [
        "buy",
        "sell",
    ]


def test_momentum_requires_volume_it_can_actually_see():
    """Missing volume used to fall straight through the filter, so the rule
    dropped its volume requirement precisely when volume was unknown — intraday,
    where the forming bar carries whatever the quote reports. Breakout already
    treated missing volume as disqualifying; both agree now."""
    bars = make_bars(uptrend())
    assert STRATEGIES["momentum"].evaluate(bars) is not None  # volume present, fires

    blind = [*bars[:-1], bars[-1].model_copy(update={"volume": None})]
    assert STRATEGIES["momentum"].evaluate(blind) is None
    assert STRATEGIES["breakout"].evaluate(blind) is None  # unchanged, for contrast


# ------------------------------------------------------------ build provenance


def test_build_version_reports_the_baked_sha():
    """A built image knows its commit; a local working tree honestly does not."""
    from trd import __version__
    from trd.build import build_version

    assert build_version(env={"TRD_GIT_SHA": "abc1234"}) == f"{__version__}+abc1234"
    assert build_version(env={}) == __version__
    assert build_version(env={"TRD_GIT_SHA": "  "}) == __version__


def test_scan_events_carry_the_build_version():
    """Groupable in Loki, so a stale rollout reads as a version that stopped
    changing rather than as behaviour that quietly went missing."""
    from trd.build import build_version
    from trd.services.engine import ScanResult, scan_events

    result = ScanResult(run_id=1, at=datetime(2026, 7, 29, 15, 55), paper=True, scanned=3)
    summary = scan_events(result)[-1]
    assert summary["ev"] == "scan"
    assert summary["version"] == build_version()


def test_missing_rules_flags_a_config_this_build_cannot_honour(monkeypatch):
    from trd.engine import exits as exit_module

    params = {**exit_module.DEFAULT_EXIT_PARAMS, "flat_at_minute": 1555.0}
    assert exit_module.missing_rules(params) == []  # this build has session_close

    # A build without the rule — exactly the state that let a day engine carry
    # positions overnight while every unit test passed.
    without = {k: v for k, v in exit_module.REGISTRY.items() if k != "session_close"}
    monkeypatch.setattr(exit_module, "REGISTRY", without)
    assert exit_module.missing_rules(params) == [("flat_at_minute", "session_close")]
    # Off is not configured: a swing engine needs no session_close.
    assert exit_module.missing_rules({**params, "flat_at_minute": 0.0}) == []


def test_scan_refuses_when_the_build_lacks_a_configured_rule(engine, provider, monkeypatch):
    """Failing loudly beats trading wrong. A day engine missing session_close does
    not error on its own — it just holds overnight, which is the one thing the
    setting exists to prevent."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], exit_params={"flat_at_minute": 1555.0})

    monkeypatch.setattr(
        "trd.services.engine.missing_rules",
        lambda params: [("flat_at_minute", "session_close")],
    )
    with pytest.raises(TrdError, match="cannot honour the configured rule set"):
        engine.scan()


def test_scan_is_unaffected_when_every_configured_rule_is_present(engine, provider):
    """The guard must not become a reason a healthy day engine stops trading."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], exit_params={"flat_at_minute": 1555.0})
    result = engine.scan()  # must not raise
    assert result.scanned == 1


# ------------------------------------------------------------ positions view


def _position_row(symbol: str, opened: datetime, max_pos: int = 5):
    from trd.models import Instrument, InstrumentType, PositionRow

    position = EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy="pullback",
        opened_at=opened,
        entry_price=Decimal("100"),
        quantity=Decimal("2"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        atr_at_entry=Decimal("2.5"),
        trail_high=Decimal("100"),
    )
    instrument = Instrument(id=1, symbol=symbol, name=symbol, type=InstrumentType.STOCK)
    return PositionRow(position=position, instrument=instrument, price=Decimal("104"))


def test_positions_table_shows_when_the_trade_was_entered():
    """A day engine takes every entry intraday and must be flat by the bell, so
    the clock — not just the date — is the interesting part."""
    from trd.cli.render import engine_positions_table

    row = _position_row("AAA", datetime(2026, 7, 30, 9, 30))
    table = engine_positions_table([row], "t", max_positions=5)
    assert "Opened" in [c.header for c in table.columns]
    rendered = _render(table)
    assert "07-30 09:30" in rendered


def test_positions_table_says_whether_the_book_is_full():
    """'Is there capacity' decides whether any new signal can be acted on, and
    used to require counting rows by hand."""
    from trd.cli.render import engine_positions_table

    rows = [_position_row(s, datetime(2026, 7, 30, 9, 30)) for s in ("AAA", "BBB")]
    caption = engine_positions_table(rows, "t", max_positions=5).caption or ""
    assert "2 of 5 open" in caption
    assert "room for 3" in caption
    assert "400.00 committed" in caption  # 2 positions x 2 shares x 100

    full = engine_positions_table(rows, "t", max_positions=2).caption or ""
    assert "no capacity" in full

    # Without a configured maximum the count still shows, the capacity cannot.
    bare = engine_positions_table(rows, "t").caption or ""
    assert "2 open" in bare and "capacity" not in bare


def _render(table) -> str:
    from rich.console import Console

    console = Console(width=200, no_color=True, record=True)
    console.print(table)
    return console.export_text()


# ------------------------------------------------------- lock-window handling


def test_quote_symbols_covers_the_universe_and_anything_held(engine, provider, conn):
    """An open position must be managed even after it leaves the watchlist, so
    the prefetch has to ask for its quote too."""
    provider.add_symbol("AAA", price="100")
    provider.add_symbol("BBB", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])
    assert engine.quote_symbols() == ["AAA"]

    account = engine.account()
    other = InstrumentRepo(conn).insert(InstrumentInfo(symbol="BBB", name="BBB"))
    engine.positions.open(
        account_id=account.id,
        instrument_id=other.id,
        signal_id=None,
        strategy="breakout",
        opened_at=datetime(2026, 7, 30, 9, 30),
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        atr_at_entry=Decimal("2.5"),
        last_bar_date=date(2026, 7, 30),
    )
    assert engine.quote_symbols() == ["AAA", "BBB"]


def test_scan_uses_prefetched_quotes_without_calling_the_provider(engine, provider, monkeypatch):
    """The whole point: the network round trip happens before the database is
    opened, so the writer lock is not held across it."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])

    def fail(*_args, **_kwargs):
        raise AssertionError("scan must not fetch quotes when they were handed in")

    monkeypatch.setattr(engine.provider, "get_quotes", fail)
    result = engine.scan(paper=True, quotes={})
    assert result.scanned == 1


def test_scan_still_fetches_its_own_quotes_when_none_are_given(engine, provider):
    """Omitting the argument must behave exactly as before."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])
    assert engine.scan(paper=True).scanned == 1


# ------------------------------------------------------------------- status


def test_status_answers_what_this_engine_is(engine, provider):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"], position_size=Decimal("500"))
    status = engine.status(db_path="/tmp/x.duckdb")

    assert status.account == DEFAULT_ENGINE_ACCOUNT
    assert status.universe == ["AAA"]
    assert status.strategies == ["breakout"]
    assert status.position_size == Decimal("500")
    assert status.db_path == "/tmp/x.duckdb"
    assert status.build  # never blank — provenance is the point
    assert status.day_mode is False


def test_status_distinguishes_a_day_engine(engine, provider):
    """The one fact that changes what every other number means: does this thing
    hold overnight?"""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], exit_params={"flat_at_minute": 1555.0})
    status = engine.status()
    assert status.day_mode is True
    assert status.flat_at_minute == 1555


def test_status_reports_capacity_and_committed(engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"], max_positions=2)
    account = engine.account()
    instrument = InstrumentRepo(conn).get_by_symbol("AAA")
    assert instrument is not None
    PriceRepo(conn).upsert_daily(instrument.id, make_bars([100.0, 110.0]))
    engine.positions.open(
        account_id=account.id,
        instrument_id=instrument.id,
        signal_id=None,
        strategy="breakout",
        opened_at=datetime(2026, 7, 30, 9, 30),
        entry_price=Decimal("100"),
        quantity=Decimal("2"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        atr_at_entry=Decimal("2.5"),
        last_bar_date=date(2026, 7, 30),
    )
    status = engine.status()
    assert status.open_positions == 1
    assert status.capacity == 1
    assert status.committed == Decimal("200")
    assert status.unrealized == Decimal("20")  # marked at the 110 close
    assert status.marks_are_stale is False


def test_status_names_symbols_too_short_to_trade(engine, provider, conn):
    """A symbol without enough history cannot fire a signal, and silence is
    indistinguishable from 'the rules said no'."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])  # breakout needs 60 bars
    instrument = InstrumentRepo(conn).get_by_symbol("AAA")
    assert instrument is not None
    PriceRepo(conn).upsert_daily(instrument.id, make_bars([100.0] * 10))

    status = engine.status()
    assert status.warmup_bars == 60
    assert status.short_history == [("AAA", 10)]
    assert status.bars_total == 10


def test_status_needs_no_network(engine, provider, monkeypatch):
    """This is the command you reach for when something is wrong — it has to
    answer when the provider is the thing that is wrong."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])

    def boom(*_a, **_k):
        raise AssertionError("status must not call the provider")

    monkeypatch.setattr(engine.provider, "get_quotes", boom)
    monkeypatch.setattr(engine.provider, "get_quote", boom)
    assert engine.status().open_positions == 0


def test_status_counts_scans(engine, provider):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])
    assert engine.status().last_scan is None
    assert engine.status().scans_today == 0

    engine.scan(paper=True)
    status = engine.status()
    assert status.last_scan is not None
    assert status.scans_today == 1


# --------------------------------------------------------------- run history


def test_run_rows_returns_scans_newest_first(engine, provider):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])
    for minute in (30, 35, 40):
        engine.scan(paper=True, at=datetime(2026, 7, 30, 9, minute))

    runs = engine.run_rows()
    assert len(runs) == 3
    assert [r.started_at.minute for r in runs] == [40, 35, 30]


def test_run_rows_today_only(engine, provider):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])
    engine.scan(paper=True, at=datetime(2020, 1, 1, 10, 0))  # ancient
    engine.scan(paper=True, at=datetime.now())

    assert len(engine.run_rows()) == 2
    assert len(engine.run_rows(today=True)) == 1


def test_runs_table_flags_a_gap_in_the_cadence():
    """A CronJob that stopped is invisible in every other view: 'the engine did
    nothing' and 'the engine never ran' look identical without the cadence."""
    from trd.cli.render import engine_runs_table
    from trd.models import EngineRun

    # Every 5 minutes, except one 40-minute hole where scans went missing.
    minutes = [0, 5, 10, 50, 55]
    runs = [
        EngineRun(id=i, started_at=datetime(2026, 7, 30, 10, m), scanned=20)
        for i, m in enumerate(minutes)
    ][::-1]  # newest first, as the repo returns them

    rendered = _render(engine_runs_table(runs))
    assert "⚠" in rendered
    assert "40m" in rendered  # the hole is named, not merely flagged
    assert "usual cadence 5m" in rendered  # the caption appears only when flagged


def test_runs_table_without_a_gap_stays_quiet():
    from trd.cli.render import engine_runs_table
    from trd.models import EngineRun

    runs = [
        EngineRun(id=i, started_at=datetime(2026, 7, 30, 10, m), scanned=20)
        for i, m in enumerate((0, 5, 10))
    ][::-1]
    assert "⚠" not in _render(engine_runs_table(runs))


# ------------------------------------------------------------------- sizing


def _sizing_bars(price: float, atr_ish: float):
    """Bars whose ATR(14) lands near `atr_ish`, so stop distance is controllable."""
    out = []
    for i in range(40):
        base = price
        out.append(
            DailyBar(
                date=date(2026, 1, 1) + timedelta(days=i),
                open=Decimal(str(base)),
                high=Decimal(str(base + atr_ish / 2)),
                low=Decimal(str(base - atr_ish / 2)),
                close=Decimal(str(base)),
                volume=1_000_000,
            )
        )
    return out


def test_exposure_mode_commits_the_same_dollars_and_lets_risk_float():
    """The original behaviour: position size constant, risk varies with the stop."""
    from trd.services.engine import plan_entry

    params = dict(exit_rules.DEFAULT_EXIT_PARAMS)
    calm, _ = plan_entry(_sizing_bars(100, 1), Decimal("1000"), params)
    jumpy, _ = plan_entry(_sizing_bars(100, 10), Decimal("1000"), params)
    assert calm is not None and jumpy is not None

    assert calm.quantity * Decimal("100") == pytest.approx(Decimal("1000"), abs=1)
    assert jumpy.quantity * Decimal("100") == pytest.approx(Decimal("1000"), abs=1)
    calm_risk = (Decimal("100") - calm.stop_price) * calm.quantity
    jumpy_risk = (Decimal("100") - jumpy.stop_price) * jumpy.quantity
    assert jumpy_risk > calm_risk * 5  # same money committed, far more at risk


def test_risk_mode_risks_the_same_dollars_and_lets_exposure_float():
    """Every trade loses the same amount at its stop, so R is a real constant."""
    from trd.models import SizingMode
    from trd.services.engine import plan_entry

    # Both wide enough that the exposure cap does not bind — that is its own test.
    params = dict(exit_rules.DEFAULT_EXIT_PARAMS)
    calm, _ = plan_entry(_sizing_bars(100, 3), Decimal("100"), params, SizingMode.RISK)
    jumpy, _ = plan_entry(_sizing_bars(100, 15), Decimal("100"), params, SizingMode.RISK)
    assert calm is not None and jumpy is not None

    calm_risk = (Decimal("100") - calm.stop_price) * calm.quantity
    jumpy_risk = (Decimal("100") - jumpy.stop_price) * jumpy.quantity
    assert calm_risk == pytest.approx(Decimal("100"), abs=1)
    assert jumpy_risk == pytest.approx(Decimal("100"), abs=1)
    # The calm name needs far more capital to put the same amount at risk.
    assert calm.quantity > jumpy.quantity * 4


def test_risk_mode_caps_the_capital_a_tight_stop_would_demand():
    """A stop 1% away would want 100x the budget in stock. Take the smaller of
    the two — under-risking is safe, over-committing is not."""
    from trd.models import SizingMode
    from trd.services.engine import MAX_EXPOSURE_MULTIPLE, plan_entry

    params = dict(exit_rules.DEFAULT_EXIT_PARAMS)
    plan, _ = plan_entry(_sizing_bars(100, 0.1), Decimal("100"), params, SizingMode.RISK)
    assert plan is not None
    exposure = plan.quantity * Decimal("100")
    assert exposure <= Decimal("100") * MAX_EXPOSURE_MULTIPLE
    risk = (Decimal("100") - plan.stop_price) * plan.quantity
    assert risk < Decimal("100")  # capped, so it risks less than the budget


def test_sizing_mode_defaults_to_exposure_and_round_trips(engine, provider):
    """Existing engines must keep behaving exactly as they did."""
    from trd.models import SizingMode

    provider.add_symbol("AAA", price="100")
    config, _, _ = engine.init(symbols=["AAA"], strategies=["breakout"])
    assert config.sizing_mode == SizingMode.EXPOSURE
    assert engine.config().sizing_mode == SizingMode.EXPOSURE

    config, _, _ = engine.init(
        symbols=["AAA"], strategies=["breakout"], sizing_mode=SizingMode.RISK
    )
    assert engine.config().sizing_mode == SizingMode.RISK
