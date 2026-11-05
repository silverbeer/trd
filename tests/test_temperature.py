"""Market temperature (SB-1538): S&P move, breadth and VIX folded into one label."""

from decimal import Decimal

from trd.engine.temperature import Temperature, market_temperature
from trd.models import Quote


def q(symbol: str, pct: float, prev: float = 100.0) -> Quote:
    return Quote(
        symbol=symbol,
        price=Decimal(str(round(prev * (1 + pct / 100), 6))),
        prev_close=Decimal(str(prev)),
    )


def book(spy: float | None, up: int, down: int, vix: float | None = None) -> tuple[dict, list]:
    quotes = {}
    names = []
    for i in range(up):
        quotes[f"U{i}"] = q(f"U{i}", 1.0)
        names.append(f"U{i}")
    for i in range(down):
        quotes[f"D{i}"] = q(f"D{i}", -1.0)
        names.append(f"D{i}")
    if spy is not None:
        quotes["SPY"] = q("SPY", spy)
    if vix is not None:
        quotes["^VIX"] = q("^VIX", vix, prev=20.0)
    return quotes, names


def test_a_big_green_day_with_broad_participation_is_hot() -> None:
    t = market_temperature(*book(spy=1.6, up=8, down=2))
    assert t.label == Temperature.HOT
    assert t.breadth_pct == Decimal(80) and t.breadth_names == 10
    assert "S&P +1.6%" in t.line() and "80% of 10 names up" in t.line()


def test_a_selloff_everyone_joins_is_cold() -> None:
    assert market_temperature(*book(spy=-1.8, up=1, down=9)).label == Temperature.COLD


def test_one_strong_reading_against_a_lukewarm_other_is_only_warm() -> None:
    # SPY +1.2% (hot) but only 62% of names up (warm): warm, not hot.
    assert market_temperature(*book(spy=1.2, up=62, down=38)).label == Temperature.WARM


def test_spy_up_while_most_names_fall_is_neutral_and_says_why() -> None:
    t = market_temperature(*book(spy=0.8, up=2, down=8))
    assert t.label == Temperature.NEUTRAL
    assert t.reason == "S&P and breadth disagree"


def test_a_vix_spike_caps_a_green_day_at_cool() -> None:
    t = market_temperature(*book(spy=1.5, up=9, down=1, vix=15.0))
    assert t.label == Temperature.COOL
    assert "VIX up 15%" in t.reason


def test_missing_inputs_are_neutral_not_guessed() -> None:
    t = market_temperature({}, [])
    assert t.label == Temperature.NEUTRAL and "no read" in t.reason
    # Too few names for breadth: SPY alone decides, and says so.
    alone = market_temperature(*book(spy=1.5, up=2, down=1))
    assert alone.label == Temperature.HOT and alone.breadth_pct is None
    assert alone.reason == "S&P alone reads hot"


def test_breadth_never_counts_spy_or_vix() -> None:
    quotes, names = book(spy=2.0, up=3, down=3, vix=-5.0)
    t = market_temperature(quotes, [*names, "SPY", "^VIX"])
    assert t.breadth_names == 6 and t.breadth_pct == Decimal(50)


# ------------------------------------------------------------- the scan records it


def test_a_scan_records_the_temperature_on_its_run(conn, provider) -> None:
    from datetime import datetime

    from tests.test_engine import make_bars, seed, uptrend
    from trd.services import EngineService

    engine = EngineService(conn, provider)
    bars = make_bars(uptrend())
    names = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    for name in names:
        provider.add_symbol(name, price="101", prev_close="100")
        seed(conn, name, bars)
    provider.add_symbol("SPY", price="101.5", prev_close="100")
    engine.init(symbols=names, strategies=["momentum"], position_size=Decimal("1000"))

    assert {"SPY", "^VIX"} <= set(engine.quote_symbols())
    result = engine.scan(at=datetime(2024, 9, 16, 10, 0))
    assert result.temperature is not None
    assert result.temperature.label == Temperature.HOT
    assert result.temperature.breadth_names == 5

    (run,) = engine.runs.list_recent(1)
    assert run.temperature == result.temperature  # round-trips through the column
    from trd.services.engine import scan_events

    assert scan_events(result)[-1]["temperature"] == "hot"
