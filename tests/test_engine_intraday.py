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
from tests.test_engine import make_bars, make_intraday_bars, seed, seed_intraday, uptrend
from trd.engine.bars import BarSource, bucket_start, day_mode_on_daily_bars
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
    """`max_bars` and `indicator_grace_bars` are counts of bars. On a 5-minute
    engine a 10-bar time stop is 50 minutes, and that is the honest reading."""
    intraday = BarSource(PriceRepo(conn), "5m")
    bars = make_intraday_bars([100.0] * 12)
    opened = bars[4].ts
    assert intraday.bars_since(list(bars), opened) == 7

    daily = BarSource(PriceRepo(conn), "1d")
    day_bars = make_bars([100.0] * 12)
    assert daily.bars_since(list(day_bars), datetime.combine(day_bars[4].date, time.min)) == 7


# -------------------------------------------------------------------- rules


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
    """Which table the ATR came from, proved by making the two disagree: the same
    symbol carries daily bars at ten times the scale, so an engine reading the
    wrong source would size a stop roughly ten times too wide."""
    closes = uptrend()
    _day_engine(engine, provider, conn, closes)
    seed(conn, "AAA", make_bars([c * 10 for c in closes]))

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
    _day_engine(engine, provider, conn, uptrend())
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
    _day_engine(engine, provider, conn, uptrend())
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
    _day_engine(engine, provider, conn, uptrend())
    engine.scan(at=datetime(2024, 9, 16, 10, 0))
    engine.scan(at=datetime(2024, 9, 16, 10, 20))

    stamps = {row.signal.bar_ts for row in engine.signal_rows(limit=20)}
    assert len(stamps) == 2
    assert {s.time() for s in stamps} == {time(10, 0), time(10, 20)}


def test_the_same_bucket_rescanned_records_one_signal(engine, provider, conn) -> None:
    """A monitor loop re-derives the same signal every pass; it is stored once."""
    _day_engine(engine, provider, conn, uptrend())
    engine.scan(at=datetime(2024, 9, 16, 10, 0))
    engine.scan(at=datetime(2024, 9, 16, 10, 3))  # same 10:00 bucket

    assert len({row.signal.bar_ts for row in engine.signal_rows(limit=20)}) == 1


# --------------------------------------------------------------------- sync


def test_sync_pulls_intraday_for_an_intraday_engine(engine, provider, conn) -> None:
    """Driven by the engine config, not a flag: a day engine with no intraday
    series takes no trades at all, and a flag makes that a thing you can forget."""
    _day_engine(engine, provider, conn, uptrend())
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
    _day_engine(engine, provider, conn, uptrend())
    status = engine.status()
    assert status.timeframe == "5m"
    assert status.day_mode is True
    assert status.bars_total == 260  # counted from price_intraday, not price_daily
    assert status.bar_unit == "5m"
