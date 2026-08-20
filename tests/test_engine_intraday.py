"""An engine running on intraday bars.

The point of the timeframe is not that the engine scans more often — it already
did. It is that the stop and the target are sized to a move the bar can actually
make, so an exit can come from the rules instead of always from the clock. The
test that matters here is `test_a_stop_can_actually_be_hit_intraday`.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from tests.test_engine import (
    _position,
    make_bars,
    make_intraday_bars,
    seed,
    seed_intraday,
    trend_daily,
    uptrend,
)
from trd.engine import REGISTRY as STRATEGIES
from trd.engine import exits as exit_rules
from trd.engine import strategies as strategies_mod
from trd.engine.bars import (
    DAILY,
    BarSource,
    bars_per_session,
    bucket_start,
    day_mode_on_daily_bars,
    sessions_to_bars,
)
from trd.engine.base import StrategyContext
from trd.engine.exits import DEFAULT_EXIT_PARAMS
from trd.errors import TrdError
from trd.models import IntradayBar, Quote
from trd.repos import PriceRepo
from trd.services import EngineService, SyncService


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _quote(price: str, volume: int | None = 1_200_000) -> Quote:
    return Quote(symbol="AAA", price=Decimal(price), volume=volume)


# ----------------------------------------------------------------- bucketing


@pytest.mark.parametrize(
    ("moment", "minutes", "expected"),
    [
        (datetime(2026, 7, 31, 9, 32), 5, datetime(2026, 7, 31, 9, 30)),
        (datetime(2026, 7, 31, 9, 35), 5, datetime(2026, 7, 31, 9, 35)),
        (datetime(2026, 7, 31, 9, 44, 59), 15, datetime(2026, 7, 31, 9, 30)),
        (datetime(2026, 7, 31, 15, 59), 60, datetime(2026, 7, 31, 15, 0)),
    ],
)
def test_bucket_start_floors_to_the_bar(moment, minutes, expected) -> None:
    assert bucket_start(moment, minutes) == expected


def test_unknown_timeframe_is_refused(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(TrdError, match="Unknown timeframe"):
        BarSource(PriceRepo(conn), "3s")


# -------------------------------------------------------------- forming bar


def test_forming_bar_refines_the_current_bucket(conn: duckdb.DuckDBPyConnection) -> None:
    source = BarSource(PriceRepo(conn), "5m")
    bars = make_intraday_bars([100.0, 101.0])
    now = bars[-1].ts + timedelta(minutes=2)  # inside the last stored bar
    folded = source.with_live_bar(list(bars), _quote("103"), now)

    assert len(folded) == len(bars)  # refined, not appended
    assert folded[-1].close == Decimal("103")
    assert folded[-1].high == Decimal("103")  # the quote made a new high
    assert folded[-1].low == bars[-1].low


def test_a_quotes_session_volume_never_reaches_an_intraday_bar(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The bug that made breakout's volume filter meaningless on 5m bars.

    A quote reports volume for the whole session — on yfinance, extended hours
    too. MU on 2026-08-04 quoted 37,253,500 against 31,528,521 traded across all
    79 regular-session 5-minute bars. Written into a five-minute forming bar,
    `volratio` compared roughly a hundred bars of volume against a one-bar
    average and reported 88x-132x, so `vol >= 1.5` passed on every candidate.

    A daily forming bar is the opposite case: session-to-date against prior full
    sessions is exactly the right unit, and must keep working.
    """
    intraday = BarSource(PriceRepo(conn), "5m")
    bars = make_intraday_bars([100.0, 101.0])

    # New bucket: no volume at all rather than the session's.
    opened = intraday.with_live_bar(
        list(bars), _quote("103", volume=37_253_500), bars[-1].ts + timedelta(minutes=7)
    )
    assert opened[-1].volume is None

    # Refined bucket: keeps the stored bar's real (partial) volume, not the quote's.
    refined = intraday.with_live_bar(
        list(bars), _quote("103", volume=37_253_500), bars[-1].ts + timedelta(minutes=2)
    )
    assert refined[-1].volume == bars[-1].volume

    # A daily engine still takes it — there the unit is right.
    daily = BarSource(PriceRepo(conn), "1d")
    day_bars = make_bars([100.0, 101.0])
    folded = daily.with_live_bar(
        list(day_bars),
        _quote("103", volume=37_253_500),
        datetime.combine(day_bars[-1].date, time(15, 0)),
    )
    assert folded[-1].volume == 37_253_500


def test_a_daily_forming_bar_takes_the_sessions_real_range(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A daily bar synced minutes after the open spans a minute, and refining it
    against `last_price` alone never recovers the rest. ATR is computed from that
    range and sizes every stop, so the sliver makes stops too tight and inflates
    R for the life of the trade.

    Measured 2026-08-05: MSFT's stored bar had low 495.71 against a session low of
    485.68 — and the correct number was sitting in the quote the whole time.
    """
    daily = BarSource(PriceRepo(conn), "1d")
    stored = make_bars([100.0, 101.0])
    sliver = stored[-1].model_copy(update={"high": Decimal("101.2"), "low": Decimal("100.9")})
    quote = Quote(
        symbol="AAA",
        price=Decimal("101"),
        day_high=Decimal("104"),
        day_low=Decimal("97"),
    )
    now = datetime.combine(sliver.date, time(15, 0))

    folded = daily.with_live_bar([*stored[:-1], sliver], quote, now)
    assert folded[-1].high == Decimal("104")
    assert folded[-1].low == Decimal("97")

    # And on a bucket the sync never wrote at all.
    opened = daily.with_live_bar(list(stored[:-1]), quote, now)
    assert opened[-1].high == Decimal("104")
    assert opened[-1].low == Decimal("97")


def test_an_intraday_forming_bar_refuses_the_session_range(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """A session high is not a 5-minute bar's high. Handing these to an intraday
    bar is the same unit error as putting a session's volume in one — see the
    quote-volume fix."""
    intraday = BarSource(PriceRepo(conn), "5m")
    bars = make_intraday_bars([100.0, 101.0])
    quote = Quote(
        symbol="AAA", price=Decimal("101"), day_high=Decimal("104"), day_low=Decimal("97")
    )

    folded = intraday.with_live_bar(list(bars), quote, bars[-1].ts + timedelta(minutes=2))
    assert folded[-1].high == max(bars[-1].high, Decimal("101"))
    assert folded[-1].low == min(bars[-1].low, Decimal("101"))
    assert folded[-1].high < Decimal("104")


def test_forming_bar_opens_a_new_bucket(conn: duckdb.DuckDBPyConnection) -> None:
    source = BarSource(PriceRepo(conn), "5m")
    bars = make_intraday_bars([100.0, 101.0])
    now = bars[-1].ts + timedelta(minutes=7)  # two buckets on
    folded = source.with_live_bar(list(bars), _quote("103"), now)

    assert len(folded) == len(bars) + 1
    newest = folded[-1]
    assert isinstance(newest, IntradayBar)
    assert newest.ts == bucket_start(now, 5)
    assert newest.open == newest.high == newest.low == newest.close == Decimal("103")


def test_a_quote_that_only_repeats_the_last_close_is_stale(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The provider answers a quote for a symbol that has not printed yet by
    handing back the prior close. Folded in, that is a bar that never traded."""
    source = BarSource(PriceRepo(conn), "5m")
    bars = make_intraday_bars([100.0, 101.0])
    later = bars[-1].ts + timedelta(minutes=30)

    assert source.quote_is_stale(list(bars), _quote("101"), later) is True
    assert source.quote_is_stale(list(bars), _quote("101.50"), later) is False
    # Inside the stored bucket the quote is refining known-good data, not inventing it.
    assert source.quote_is_stale(list(bars), _quote("101"), bars[-1].ts) is False


def test_bars_held_counts_bars_not_days(conn: duckdb.DuckDBPyConnection) -> None:
    """A trade's age is counted in bars — what the engine can see. The thresholds
    it is measured against are sessions (`max_sessions`, `indicator_grace_sessions`),
    resolved to bars by `sessions_to_bars`; see SB-600."""
    intraday = BarSource(PriceRepo(conn), "5m")
    bars = make_intraday_bars([100.0] * 12)
    opened = bars[4].ts
    assert intraday.bars_since(list(bars), opened) == 7

    daily = BarSource(PriceRepo(conn), "1d")
    day_bars = make_bars([100.0] * 12)
    assert daily.bars_since(list(day_bars), datetime.combine(day_bars[4].date, time.min)) == 7


# -------------------------------------------------------------------- rules


def _day_closes(n: int = 1700) -> list[float]:
    """A 5-minute close series long enough to satisfy a session-denominated
    warmup: momentum's 20-session trigger lookback is 1,561 bars at this width.
    The drift is intraday-scale — the daily-bar default compounds to +640% over
    the same span and pins RSI at the top of its range."""
    return uptrend(n=n, drift=0.0002, wobble=0.6)


def _day_engine(engine: EngineService, provider: FakeProvider, conn, closes) -> None:
    bars = make_intraday_bars(closes)
    seed_intraday(conn, "AAA", bars)
    # Not exactly the last close: a quote that only repeats it is the "has not
    # printed yet" case, and the scanner refuses to fill on it.
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine.init(
        symbols=["AAA"],
        strategies=["momentum"],
        position_size=Decimal("10000"),
        exit_params={"flat_at_minute": 1555.0},
        timeframe="5m",
    )


def test_a_day_engine_needs_an_intraday_timeframe(engine, provider) -> None:
    """The bug this whole change exists to fix: on daily bars a 2 x ATR stop
    cannot be reached inside one session, so every trade exits on the clock and
    the R-multiples describe a risk profile the engine never ran."""
    provider.add_symbol("AAA", price="100")
    with pytest.raises(TrdError, match="needs an intraday timeframe"):
        engine.init(symbols=["AAA"], exit_params={"flat_at_minute": 1555.0})


def test_the_refusal_is_one_function_init_and_status_share(engine, provider) -> None:
    """The guard used to live only in init, so an engine created before it kept
    running in the refused state and nothing said so. One function now answers
    'is this pairing legal', and both callers append their own remedy."""
    assert day_mode_on_daily_bars("1d", 1555) is not None
    assert day_mode_on_daily_bars("5m", 1555) is None  # intraday day engine — fine
    assert day_mode_on_daily_bars("1d", 0) is None  # swing engine on daily — fine


def test_status_flags_an_engine_already_running_in_the_refused_config(
    engine, provider, conn
) -> None:
    """The regression: `day-sim` ran for weeks on daily bars with --flat-at set,
    every trade exiting on the clock, and `engine status` reported it as healthy.
    init cannot catch this — the engine already exists."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], exit_params={"flat_at_minute": 1555.0}, timeframe="5m")
    assert engine.status().config_refused is None

    # Exactly what an engine created before the guard looks like on disk.
    conn.execute("UPDATE engine_config SET timeframe = '1d'")

    refused = engine.status().config_refused
    assert refused is not None
    assert "needs an intraday timeframe" in refused
    assert "already running that way" in refused
    assert "5m/15m/30m/1h" in refused  # names the way out, not just the problem


def test_a_healthy_engine_says_nothing(engine, provider) -> None:
    """A warning that appears on sound configurations is a warning people learn
    to skip past."""
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], timeframe="1d")
    assert engine.status().config_refused is None


def test_a_swing_engine_still_defaults_to_daily(engine, provider) -> None:
    provider.add_symbol("AAA", price="100")
    config, _account, _universe = engine.init(symbols=["AAA"])
    assert config.timeframe == "1d"
    assert config.is_intraday is False


def test_the_stop_is_sized_from_the_intraday_series(engine, provider, conn) -> None:
    """Which table the ATR came from, proved by making the two disagree.

    The daily bars trace the same trend at the same price level — they have to,
    because the trend filter reads them, and an instrument priced ten times
    higher on one table than the other is not a disagreement about ATR, it is
    two different companies. What differs is the *range*: each daily bar spans
    20%, so daily ATR is an order of magnitude wider than the 5-minute one. An
    engine sizing its stop off the wrong table would set it far outside the
    ceiling asserted below.
    """
    closes = _day_closes()
    _day_engine(engine, provider, conn, closes)
    first_session = make_intraday_bars(closes)[0].ts.date()
    wide = [
        b.model_copy(update={"high": b.close * Decimal("1.1"), "low": b.close * Decimal("0.9")})
        for b in trend_daily(closes[0], last_session=first_session)
    ]
    seed(conn, "AAA", wide)

    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(result.opened) == 1
    position = engine.position_rows(open_only=True)[0].position

    entry = float(position.entry_price)
    stop_distance = entry - float(position.stop_price)
    # A 2 x ATR stop on this series sits a few percent below the entry. Off the
    # daily bars it would be an order of magnitude wider than the price itself.
    assert 0 < stop_distance < entry * 0.25


def test_a_stop_can_actually_be_hit_intraday(engine, provider, conn) -> None:
    """The whole point. Open a trade, drop the quote below the stop, and the exit
    comes from `stop` — not from `session_close` hours later."""
    _day_engine(engine, provider, conn, _day_closes())
    opened = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(opened.opened) == 1
    position = engine.position_rows(open_only=True)[0].position

    # A price through the stop, well before the flat time.
    provider.add_symbol("AAA", price=str(float(position.stop_price) * 0.99), volume=1_200_000)
    closed = engine.scan(at=datetime(2024, 9, 16, 11, 0))
    assert len(closed.closed) == 1
    assert closed.closed[0].rule == "stop"
    assert engine.position_rows(open_only=True) == []


def test_a_target_can_actually_be_hit_intraday(engine, provider, conn) -> None:
    _day_engine(engine, provider, conn, _day_closes())
    opened = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert len(opened.opened) == 1
    position = engine.position_rows(open_only=True)[0].position

    provider.add_symbol("AAA", price=str(float(position.target_price) * 1.01), volume=1_200_000)
    closed = engine.scan(at=datetime(2024, 9, 16, 11, 0))
    assert len(closed.closed) == 1
    assert closed.closed[0].rule in {"target", "trail"}


def test_signals_are_recorded_per_bucket_not_per_session(engine, provider, conn) -> None:
    """A 5-minute session is 78 buckets. Recording one signal a day would throw
    away the audit trail `trd engine signals` exists to keep."""
    _day_engine(engine, provider, conn, _day_closes())
    engine.scan(at=datetime(2024, 9, 16, 10, 0))
    engine.scan(at=datetime(2024, 9, 16, 10, 20))

    stamps = {row.signal.bar_ts for row in engine.signal_rows(limit=20)}
    assert len(stamps) == 2
    assert {s.time() for s in stamps} == {time(10, 0), time(10, 20)}


def test_the_same_bucket_rescanned_records_one_signal(engine, provider, conn) -> None:
    """A monitor loop re-derives the same signal every pass; it is stored once."""
    _day_engine(engine, provider, conn, _day_closes())
    engine.scan(at=datetime(2024, 9, 16, 10, 0))
    engine.scan(at=datetime(2024, 9, 16, 10, 3))  # same 10:00 bucket

    assert len({row.signal.bar_ts for row in engine.signal_rows(limit=20)}) == 1


# --------------------------------------------------------------------- sync


def test_sync_pulls_intraday_for_an_intraday_engine(engine, provider, conn) -> None:
    """Driven by the engine config, not a flag: a day engine with no intraday
    series takes no trades at all, and a flag makes that a thing you can forget."""
    _day_engine(engine, provider, conn, _day_closes())
    fetched = make_intraday_bars([100.0, 101.0, 102.0])
    provider.add_intraday("AAA", "5m", fetched)

    count, timeframe = SyncService(conn, provider).sync_intraday(
        now=datetime.combine(fetched[-1].ts.date(), time(16, 0))
    )
    assert timeframe == "5m"
    assert count == 3


def test_sync_leaves_a_swing_engine_alone(engine, provider, conn) -> None:
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"])
    provider.add_intraday("AAA", "5m", make_intraday_bars([100.0, 101.0]))

    assert SyncService(conn, provider).sync_intraday() == (0, None)


def test_intraday_backfill_resumes_from_the_newest_bar(conn: duckdb.DuckDBPyConnection) -> None:
    """One session of overlap: the newest stored bar is usually the one that was
    still forming when it was written, so re-fetching its session settles it."""
    source = BarSource(PriceRepo(conn), "5m")
    now = datetime(2026, 7, 31, 12, 0)

    cold_start, cold_end = source.backfill_window(None, now)
    assert cold_end == date(2026, 8, 1)
    assert (cold_end - cold_start).days == 59

    warm_start, warm_end = source.backfill_window(datetime(2026, 7, 30, 15, 55), now)
    assert warm_start == date(2026, 7, 30)  # that session again, not the one after
    assert warm_end == date(2026, 8, 1)


# ------------------------------------------------------------------- status


def test_status_reports_the_timeframe_and_intraday_depth(engine, provider, conn) -> None:
    _day_engine(engine, provider, conn, _day_closes())
    status = engine.status()
    assert status.timeframe == "5m"
    assert status.day_mode is True
    assert status.bars_total == 1700  # counted from price_intraday, not price_daily
    assert status.bar_unit == "5m"


# ------------------------------------------- session-scaled lookbacks (SB-600)


@pytest.mark.parametrize(
    ("timeframe", "per_session"),
    [("1d", 1), ("5m", 78), ("15m", 26), ("30m", 13), ("1h", 7)],
)
def test_a_session_resolves_to_the_right_number_of_bars(timeframe, per_session) -> None:
    """390 minutes of regular session, divided by the bar width. 1h rounds up to
    seven because the provider emits a short final bar, and flooring to six would
    quietly shorten every 1h lookback."""
    assert bars_per_session(timeframe) == per_session
    assert sessions_to_bars(timeframe, 20) == 20 * per_session


def test_scaling_is_the_identity_on_a_swing_engine() -> None:
    """The whole safety property of SB-600: daily engines are untouched, because
    on daily bars a session *is* a bar."""
    for sessions in (0, 1, 3, 10, 20, 200):
        assert sessions_to_bars(DAILY, sessions) == sessions


def test_zero_sessions_stays_zero() -> None:
    """`max_sessions: 0` means close on the next bar. Rounding it up to one bar
    would silently disable the tests that use it to force a same-bar exit."""
    assert sessions_to_bars("5m", 0) == 0
    assert sessions_to_bars(DAILY, 0) == 0


def test_the_indicator_exit_no_longer_sells_a_five_minute_trade_in_fifteen_minutes() -> None:
    """The bug this ticket exists for.

    `indicator_grace_sessions` is 3. Read as bars that was 15 minutes on a 5m
    engine, so a fresh entry was eligible to be sold three bars in — and 228 of
    the live day engine's 407 trades were, against a "20-day" average that was
    really 100 minutes. Three sessions is 234 bars, so the rule stays dormant.
    """
    # Long enough for a 20-session average (1,560 bars) to exist at all. Below
    # that the indicator returns None and the rule declines — the safe direction,
    # and the reason a freshly-added symbol simply produces no indicator exit
    # until it has the history.
    closes = uptrend(n=2200)
    window = sessions_to_bars("5m", 20)
    # The weakness goes in the settled closes, where this rule now reads it —
    # a low quote alone no longer arms it (SB-784).
    closes[-1] = sum(closes[-window:]) / window * 0.9
    bars = make_intraday_bars(closes)
    params = dict(DEFAULT_EXIT_PARAMS)
    rule = exit_rules.IndicatorExit()
    below = bars[-1].close
    now = bars[-1].ts

    # three bars in — what used to be enough to arm the rule
    assert rule.check(_position(bars_held=3), bars, bars, below, params, now, "5m") is None
    # three *sessions* in, the threshold it was always meant to be
    armed = rule.check(_position(bars_held=234), bars, bars, below, params, now, "5m")
    assert armed is not None and armed.rule == "indicator"
    assert "20-session" in armed.reason
    assert "20-day" not in armed.reason  # the string that lied in 228 trade records


def test_the_time_exit_no_longer_fires_after_fifty_minutes() -> None:
    """`max_sessions` is 10. As bars that was 50 minutes on a 5m engine, and 84 of
    the live day engine's trades closed at exactly that. Ten sessions is 780."""
    bars = make_intraday_bars(uptrend(n=400))
    params = dict(DEFAULT_EXIT_PARAMS)
    rule = exit_rules.TimeExit()
    now = bars[-1].ts

    assert (
        rule.check(_position(bars_held=10), bars, bars, Decimal("101"), params, now, "5m") is None
    )
    hit = rule.check(_position(bars_held=780), bars, bars, Decimal("101"), params, now, "5m")
    assert hit is not None and hit.rule == "time"
    assert "10 sessions" in hit.reason


def test_the_same_thresholds_still_fire_on_schedule_for_a_swing_engine() -> None:
    """The regression guard in the other direction: a daily engine must behave
    exactly as it did before the rename."""
    bars = make_bars(uptrend())
    params = dict(DEFAULT_EXIT_PARAMS)
    now = datetime(2024, 9, 16, 12, 0)

    assert (
        exit_rules.TimeExit().check(
            _position(bars_held=9), bars, bars, Decimal("101"), params, now, DAILY
        )
        is None
    )
    hit = exit_rules.TimeExit().check(
        _position(bars_held=10), bars, bars, Decimal("101"), params, now, DAILY
    )
    assert hit is not None and hit.rule == "time"


# --------------------------------------- session-scaled entry rules (SB-607)


@pytest.mark.parametrize(
    ("timeframe", "per_session"),
    [("1d", 1), ("5m", 78), ("15m", 26), ("30m", 13), ("1h", 7)],
)
def test_entry_indicator_periods_are_denominated_in_sessions(
    monkeypatch, timeframe, per_session
) -> None:
    """The entry-side twin of SB-600.

    Exits were moved to session lookbacks; entries were left as raw bar counts,
    so a 5-minute engine's "200-day trend filter" was 200 bars — 2.6 sessions —
    and every description the rules print was wrong about what had been checked.
    This asserts the periods the strategies actually *ask* for, because the
    number is otherwise invisible: a rule that reads 14 bars and one that reads
    14 sessions both return a signal, and only one of them means what it says.
    """
    asked: list[tuple[str, dict, int]] = []
    real = strategies_mod.indicator

    def spy(key, bars, **params):
        asked.append((key, params, len(bars)))
        return real(key, bars, **params)

    monkeypatch.setattr(strategies_mod, "indicator", spy)

    bars = (
        make_intraday_bars(_day_closes(), minutes=5)
        if timeframe != DAILY
        else make_bars(uptrend(400))
    )
    daily = make_bars(uptrend(400))
    STRATEGIES["momentum"].evaluate(StrategyContext(bars=bars, daily=daily, timeframe=timeframe))

    periods = {key: params for key, params, _ in asked}
    assert periods["rsi"]["period"] == 14 * per_session
    assert periods["volratio"]["period"] == 20 * per_session
    # The trend filter is the exception that makes the rest possible: it reads
    # the daily series, so its period stays in sessions whatever the engine runs.
    trend_lengths = {length for key, params, length in asked if key == "sma"}
    assert trend_lengths == {len(daily)}
    assert {params["period"] for key, params, _ in asked if key == "sma"} == {50, 200}


def test_the_trend_filter_reads_daily_bars_not_the_engines_own() -> None:
    """Which series the trend came from, proved by making the two disagree.

    A 200-session trend filter cannot be computed from a 5-minute engine's own
    bars at all — 200 sessions is 15,600 of them and the provider serves about
    4,600 — so it reads daily history. Here the intraday series is climbing hard
    while the daily history is in a downtrend: momentum must decline, because the
    trend it filters on is the daily one.
    """
    bars = make_intraday_bars(_day_closes())
    # Both daily histories arrive at the level the intraday series starts from —
    # same instrument, same price — so the only thing that differs is the trend.
    level, session = float(bars[0].close), bars[0].ts.date()
    rising = StrategyContext(
        bars=bars, daily=trend_daily(level, last_session=session), timeframe="5m"
    )
    falling = StrategyContext(
        bars=bars,
        daily=trend_daily(level, last_session=session, rising=False),
        timeframe="5m",
    )

    assert STRATEGIES["momentum"].evaluate(rising) is not None
    assert STRATEGIES["momentum"].evaluate(falling) is None


@pytest.mark.parametrize("timeframe", ["1d", "5m", "15m", "30m", "1h"])
def test_every_strategy_warms_up_in_sessions_on_every_timeframe(timeframe) -> None:
    """Warmup has to scale with the rules, or a correctly-scaled rule sits behind
    a bar count that lets it fire before its own lookback exists."""
    for key, strategy in STRATEGIES.items():
        bars = strategy.warmup_bars(timeframe)
        assert bars == sessions_to_bars(timeframe, strategy.signal_sessions) + 1 or (
            timeframe == DAILY
        ), key
        if timeframe == DAILY:
            # One series on a swing engine, so warmup is whichever lookback is
            # longer — there is no second place for the trend to come from.
            assert strategy.warmup_daily(timeframe) == 0, key
            assert bars == max(strategy.trend_sessions, strategy.signal_sessions) + 1, key
        else:
            assert strategy.warmup_daily(timeframe) == strategy.trend_sessions + 1, key


# --------------------------------------------------- SB-788: when a bar settles


def test_a_daily_bar_settles_at_the_bell_not_at_midnight() -> None:
    """SB-784 made indicator exits read settled bars; measured against the
    *bucket*, a daily bar stayed unsettled until midnight, so an exit belonging
    to today's close landed on tomorrow's 09:30 scan — a full session of exposure
    on the rule that closes half of all trades."""
    source = BarSource(None, DAILY)
    bars = make_bars(uptrend(n=3))
    today = bars[-1].date

    during = datetime.combine(today, time(11, 0))
    assert source.settled(bars, during) == bars[:-1]  # today still moving

    at_the_bell = datetime.combine(today, time(16, 0))
    assert source.settled(bars, at_the_bell) == bars  # done, and readable


def test_a_settled_bar_does_not_depend_on_which_scan_runs() -> None:
    """Nothing is keyed to the 16:00 pass. An evicted pod or a busy database
    costs nothing: the next scan, whenever it comes, sees the same settled bar —
    which is what stops a missed final scan from silently deferring a session."""
    source = BarSource(None, DAILY)
    bars = make_bars(uptrend(n=3))
    today = bars[-1].date

    for missed in (
        datetime.combine(today, time(16, 5)),  # a late pass
        datetime.combine(today, time(23, 59)),  # nothing ran all evening
        datetime.combine(today + timedelta(days=1), time(9, 30)),  # next morning
    ):
        assert source.settled(bars, missed) == bars


def test_a_half_day_settles_on_the_clock_and_reads_the_real_close() -> None:
    """The market closes at 13:00 several times a year and there is deliberately
    no calendar in this engine. The scans keep running on the clock, so the bar
    settles at 16:00 carrying the 13:00 close — the correct number, later than
    ideal, rather than a wrong one or a deferred session."""
    source = BarSource(None, DAILY)
    bars = make_bars(uptrend(n=3))
    today = bars[-1].date

    assert source.settled(bars, datetime.combine(today, time(13, 30))) == bars[:-1]
    settled = source.settled(bars, datetime.combine(today, time(16, 0)))
    assert settled == bars
    assert settled[-1].close == bars[-1].close  # the 13:00 close, unaltered


def test_a_holiday_settles_nothing_new_so_nothing_can_fire() -> None:
    """No bar is stored for a holiday at all, so the newest settled bar stays
    yesterday's — the same reading yesterday already had, which cannot fire
    anything new."""
    source = BarSource(None, DAILY)
    bars = make_bars(uptrend(n=3))
    holiday = bars[-1].date + timedelta(days=1)

    assert source.settled(bars, datetime.combine(holiday, time(12, 0))) == bars


def test_an_intraday_bar_settles_one_width_after_its_stamp() -> None:
    """The intraday path is unchanged by SB-788: a 5-minute engine was never a
    session behind, and the fix must not disturb it."""
    source = BarSource(None, "5m")
    bars = make_intraday_bars(uptrend(n=4))
    last = bars[-1].ts

    assert source.settled(bars, last + timedelta(minutes=4)) == bars[:-1]
    assert source.settled(bars, last + timedelta(minutes=5)) == bars


def test_the_indicator_exit_now_fires_on_the_session_it_belongs_to() -> None:
    """The whole point of SB-788, end to end: a close below the 20-session
    average is acted on at the bell, not deferred to the next morning."""
    closes = uptrend()
    sma20 = sum(closes[-20:]) / 20
    closes[-1] = sma20 * 0.9
    bars = make_bars(closes)
    source = BarSource(None, DAILY)
    today = bars[-1].date
    rule = exit_rules.IndicatorExit()
    params = dict(DEFAULT_EXIT_PARAMS)

    midday = datetime.combine(today, time(11, 0))
    assert (
        rule.check(
            _position(bars_held=3),
            bars,
            source.settled(bars, midday),
            bars[-1].close,
            params,
            midday,
            DAILY,
        )
        is None
    )

    bell = datetime.combine(today, time(16, 0))
    fired = rule.check(
        _position(bars_held=3),
        bars,
        source.settled(bars, bell),
        bars[-1].close,
        params,
        bell,
        DAILY,
    )
    assert fired is not None and fired.rule == "indicator"
