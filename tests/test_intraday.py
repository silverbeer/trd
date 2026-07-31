"""Intraday bar storage and the bar protocol the rules run on.

The point of this layer is that a 5-minute series is just another series: the same
indicator registry, the same strategies, the same exit math, with nothing
reimplemented for the shorter timeframe. The protocol tests are what hold that
line — if a strategy ever reaches for `.date`, they fail.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trd.db.connection import connect
from trd.engine import REGISTRY as STRATEGIES
from trd.indicators import REGISTRY as INDICATORS
from trd.models import DailyBar, InstrumentInfo, IntradayBar
from trd.repos import InstrumentRepo, PriceRepo

from .conftest import FakeProvider


def _series(n: int, start: str = "100", step: str = "1") -> list[IntradayBar]:
    """A rising 5-minute series starting at 09:30 on a single session."""
    base = datetime(2026, 7, 31, 9, 30)
    bars: list[IntradayBar] = []
    for i in range(n):
        close = Decimal(start) + Decimal(step) * i
        bars.append(
            IntradayBar(
                ts=base + timedelta(minutes=5 * i),
                open=close - Decimal("0.20"),
                high=close + Decimal("0.30"),
                low=close - Decimal("0.40"),
                close=close,
                volume=1_000 + i,
            )
        )
    return bars


@pytest.fixture
def repo(tmp_path: Path) -> PriceRepo:
    return PriceRepo(connect(tmp_path / "t.duckdb"))


@pytest.fixture
def instrument_id(repo: PriceRepo) -> int:
    return InstrumentRepo(repo.conn).insert(InstrumentInfo(symbol="NVDA")).id


# ------------------------------------------------------------------- storage


def test_migration_creates_table(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.duckdb")
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert "price_intraday" in tables


def test_roundtrip_preserves_precision(repo: PriceRepo, instrument_id: int) -> None:
    bars = _series(3)
    assert repo.upsert_intraday(instrument_id, "5m", bars) == 3
    stored = repo.intraday_bars(instrument_id, "5m")
    assert [b.ts for b in stored] == [b.ts for b in bars]
    assert stored[0].close == Decimal("100")
    assert stored[0].open == Decimal("99.80")  # Decimal end to end, never float


def test_upsert_replaces_same_bar(repo: PriceRepo, instrument_id: int) -> None:
    """A forming bar is re-fetched all session — the last write must win, not
    stack up duplicates of the same instant."""
    first = _series(1)
    repo.upsert_intraday(instrument_id, "5m", first)
    revised = [first[0].model_copy(update={"close": Decimal("105")})]
    repo.upsert_intraday(instrument_id, "5m", revised)
    stored = repo.intraday_bars(instrument_id, "5m")
    assert len(stored) == 1
    assert stored[0].close == Decimal("105")


def test_intervals_are_separate_series(repo: PriceRepo, instrument_id: int) -> None:
    repo.upsert_intraday(instrument_id, "5m", _series(3))
    repo.upsert_intraday(instrument_id, "15m", _series(2, start="200"))
    assert len(repo.intraday_bars(instrument_id, "5m")) == 3
    assert len(repo.intraday_bars(instrument_id, "15m")) == 2
    assert repo.intraday_bars(instrument_id, "15m")[0].close == Decimal("200")


def test_limit_takes_the_newest_bars_oldest_first(repo: PriceRepo, instrument_id: int) -> None:
    """The rules look backwards from now, so a limit must trim the *old* end —
    and still hand back an ascending series, or every indicator reads reversed."""
    repo.upsert_intraday(instrument_id, "5m", _series(10))
    tail = repo.intraday_bars(instrument_id, "5m", limit=3)
    assert [b.close for b in tail] == [Decimal("107"), Decimal("108"), Decimal("109")]


def test_coverage_and_counts(repo: PriceRepo, instrument_id: int) -> None:
    repo.upsert_intraday(instrument_id, "5m", _series(4))
    count, first, last = repo.intraday_coverage("5m")
    assert count == 4
    assert first == datetime(2026, 7, 31, 9, 30)
    assert last == datetime(2026, 7, 31, 9, 45)
    assert repo.intraday_bar_counts("5m") == {instrument_id: 4}
    assert repo.intraday_coverage("15m") == (0, None, None)


def test_latest_ts_marks_the_resume_point(repo: PriceRepo, instrument_id: int) -> None:
    assert repo.latest_intraday_ts(instrument_id, "5m") is None
    repo.upsert_intraday(instrument_id, "5m", _series(4))
    assert repo.latest_intraday_ts(instrument_id, "5m") == datetime(2026, 7, 31, 9, 45)


def test_daily_and_intraday_do_not_mix(repo: PriceRepo, instrument_id: int) -> None:
    """Separate tables on purpose — a decade of daily must not be dragged through
    every intraday read, and vice versa."""
    repo.upsert_daily(
        instrument_id,
        [
            DailyBar(
                date=date(2026, 7, 31),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=1,
            )
        ],
    )
    repo.upsert_intraday(instrument_id, "5m", _series(3))
    assert len(repo.daily_bars(instrument_id)) == 1
    assert len(repo.intraday_bars(instrument_id, "5m")) == 3


# ------------------------------------------------------------- bar protocol


def test_every_indicator_runs_on_intraday_bars() -> None:
    """The whole design rests on this: no indicator reads a bar's calendar stamp,
    so the registry works unchanged on a 5-minute series."""
    bars = _series(260)  # clears the longest warm-up in the registry
    for key, ind in INDICATORS.items():
        series = ind.compute(bars, **ind.default_params)
        assert series, f"{key} returned nothing on intraday bars"
        assert all(len(v) == len(bars) for v in series.values()), f"{key} misaligned"


def test_every_indicator_interprets_intraday_bars() -> None:
    bars = _series(260)
    for key, ind in INDICATORS.items():
        series = ind.compute(bars, **ind.default_params)
        assert ind.interpret(series, bars), f"{key} produced no reading"


def test_every_strategy_evaluates_intraday_bars() -> None:
    """Strategies must accept the series without raising. Whether a rule fires on
    a synthetic ramp is not the point — that it can *see* it is."""
    bars = _series(260)
    for key, strategy in STRATEGIES.items():
        strategy.evaluate(bars)  # must not raise on a non-daily series
        assert strategy.min_bars > 0, f"{key} has no warm-up"


def test_daily_bars_still_satisfy_the_protocol() -> None:
    daily = [
        DailyBar(
            date=date(2026, 7, 1) + timedelta(days=i),
            open=Decimal("100"),
            high=Decimal("102"),
            low=Decimal("99"),
            close=Decimal("100") + i,
            volume=1_000,
        )
        for i in range(260)
    ]
    rsi = INDICATORS["rsi"]
    assert rsi.compute(daily, **rsi.default_params)


# ---------------------------------------------------------------- provider


def test_fake_provider_serves_intraday_in_window() -> None:
    fake = FakeProvider()
    fake.add_intraday("NVDA", "5m", _series(5))
    got = fake.get_intraday_bars("NVDA", "5m", date(2026, 7, 31), date(2026, 8, 1))
    assert len(got) == 5
    assert fake.get_intraday_bars("NVDA", "5m", date(2026, 8, 1), date(2026, 8, 2)) == []


def test_fake_provider_unknown_symbol_raises() -> None:
    from trd.errors import ProviderError

    fake = FakeProvider()
    with pytest.raises(ProviderError):
        fake.get_intraday_bars("NOPE", "5m", date(2026, 7, 1), date(2026, 8, 1))


def test_fake_provider_unknown_interval_is_empty_not_an_error() -> None:
    """A symbol with no series at that interval has no bars — it is not a failure,
    the same way an ETF with no earnings is not a failure."""
    fake = FakeProvider()
    fake.add_intraday("NVDA", "5m", _series(5))
    assert fake.get_intraday_bars("NVDA", "15m", date(2026, 7, 31), date(2026, 8, 1)) == []
