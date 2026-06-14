from datetime import date, timedelta
from decimal import Decimal

import pytest

from trd.cli.render import sunday_prep_markdown
from trd.models import DailyBar, EarningsDate
from trd.services.sunday_prep import SundayPrepService

from .conftest import FakeProvider

# A Sunday — Sunday Prep should target the coming Mon-Fri (Jun 15-19, 2026).
SUNDAY = date(2026, 6, 14)


def _series(end: date, days: int, start_price: float, daily_gain: float) -> list[DailyBar]:
    bars: list[DailyBar] = []
    price = start_price
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        value = Decimal(str(round(price, 4)))
        bars.append(
            DailyBar(
                date=d,
                open=value,
                high=Decimal(str(round(price * 1.01, 4))),
                low=Decimal(str(round(price * 0.99, 4))),
                close=value,
                volume=1_000_000,
                adj_close=value,
            )
        )
        price += daily_gain
    return bars


@pytest.fixture
def prep_provider() -> FakeProvider:
    fake = FakeProvider()
    # Futures: RTY makes an outsized move; the rest are quiet.
    fake.add_symbol("ES=F", price="5000", prev_close="4995")
    fake.add_symbol("NQ=F", price="18000", prev_close="17990")
    fake.add_symbol("YM=F", price="40000", prev_close="39980")
    fake.add_symbol("RTY=F", price="100", prev_close="98")  # +2.04% -> unusual
    fake.add_symbol("^VIX", price="12.00", prev_close="12.50")  # low -> complacency

    # Commodities: gold quiet, WTI makes an outsized (>2%) move.
    fake.add_symbol("CL=F", price="80", prev_close="77")  # +3.9% -> unusual
    fake.add_symbol("BZ=F", price="84", prev_close="83.5")
    fake.add_symbol("GC=F", price="2400", prev_close="2395")

    # Sectors: XLK strongest, XLU weakest; others in between.
    fake.add_bars("XLK", _series(SUNDAY, 8, 100, 1.0))  # ~ +7% over the week
    fake.add_bars("XLU", _series(SUNDAY, 8, 100, -0.8))  # negative
    for sym in ("XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLC"):
        fake.add_bars(sym, _series(SUNDAY, 8, 100, 0.2))

    # Index proxies: a full year+ of bars so SMA200 / 52w / ATR all compute.
    for sym in ("SPY", "QQQ", "IWM"):
        fake.add_bars(sym, _series(SUNDAY, 300, 400, 0.5))

    # Curated-universe earnings landing inside the target week.
    fake.set_earnings("NVDA", [EarningsDate(date=date(2026, 6, 17))])
    fake.set_earnings("AAPL", [EarningsDate(date=date(2026, 6, 18))])
    # One outside the window — must be excluded.
    fake.set_earnings("MSFT", [EarningsDate(date=date(2026, 7, 1))])
    return fake


@pytest.fixture
def briefing(prep_provider: FakeProvider):
    return SundayPrepService(prep_provider).build(SUNDAY)


def test_week_window_targets_coming_trading_week(briefing) -> None:
    assert briefing.week_start == date(2026, 6, 15)  # Monday
    assert briefing.week_end == date(2026, 6, 19)  # Friday


def test_week_window_on_a_weekday_uses_this_week() -> None:
    # Wednesday Jun 17 -> same Mon-Fri week.
    start, end = SundayPrepService._week_window(date(2026, 6, 17))
    assert start == date(2026, 6, 15)
    assert end == date(2026, 6, 19)


def test_futures_snapshot_flags_outsized_move(briefing) -> None:
    by_symbol = {f.symbol: f for f in briefing.futures}
    assert set(by_symbol) == {"ES=F", "NQ=F", "YM=F", "RTY=F"}
    assert by_symbol["RTY=F"].unusual is True
    assert by_symbol["ES=F"].unusual is False
    assert by_symbol["RTY=F"].change_pct > Decimal("2")


def test_commodities_snapshot(briefing) -> None:
    by_symbol = {c.symbol: c for c in briefing.commodities}
    assert set(by_symbol) == {"CL=F", "BZ=F", "GC=F"}
    assert by_symbol["CL=F"].unusual is True  # +3.9% > 2% commodity threshold
    assert by_symbol["GC=F"].unusual is False
    assert by_symbol["GC=F"].price == Decimal("2400.00")


def test_econ_events_include_fomc_in_window(briefing) -> None:
    names = [e.name for e in briefing.econ_events]
    assert any("FOMC" in n for n in names)
    # All events fall inside the target week.
    assert all(briefing.week_start <= e.date <= briefing.week_end for e in briefing.econ_events)


def test_earnings_filtered_to_week(briefing) -> None:
    symbols = {e.symbol for e in briefing.earnings}
    assert "NVDA" in symbols
    assert "AAPL" in symbols
    assert "MSFT" not in symbols  # reports Jul 1, outside the week
    nvda = next(e for e in briefing.earnings if e.symbol == "NVDA")
    assert nvda.day == "Wednesday"


def test_sector_leadership_ranking(briefing) -> None:
    assert briefing.sector_leaders[0].symbol == "XLK"
    assert briefing.sector_leaders[0].week_pct > 0
    assert briefing.sector_laggards[0].symbol == "XLU"
    assert briefing.sector_laggards[0].week_pct < 0


def test_volatility_band(briefing) -> None:
    assert briefing.volatility.vix == Decimal("12.00")
    assert "complacency" in briefing.volatility.band


def test_key_levels_compute_full_history(briefing) -> None:
    assert {lvl.symbol for lvl in briefing.key_levels} == {"SPY", "QQQ", "IWM"}
    spy = next(lvl for lvl in briefing.key_levels if lvl.symbol == "SPY")
    assert spy.sma50 is not None
    assert spy.sma200 is not None  # 300 bars > 200
    assert spy.high52 is not None and spy.low52 is not None
    assert spy.atr is not None
    assert spy.note


def test_themes_and_risks_reflect_fomc(briefing) -> None:
    assert any("rate" in t.title.lower() for t in briefing.themes)
    assert any("FOMC" in r for r in briefing.risks)
    assert briefing.themes  # never empty
    assert briefing.risks


def test_watchlist_bounded_and_seeded(briefing) -> None:
    assert 1 <= len(briefing.watchlist) <= 10
    symbols = {w.symbol for w in briefing.watchlist}
    assert "XLK" in symbols  # top sector surfaces
    # Earnings names appear as catalysts.
    assert any("Earnings" in w.catalyst for w in briefing.watchlist)


def test_tone_and_coaching_present(briefing) -> None:
    assert briefing.tone
    assert "plan before the market opens" in briefing.mindset
    assert briefing.prompt_question.endswith("?")


def test_missing_data_degrades_gracefully() -> None:
    # Empty provider: no quotes, no bars, no earnings — must still build a briefing.
    briefing = SundayPrepService(FakeProvider()).build(SUNDAY)
    assert briefing.futures  # rows exist even with None prices
    assert all(f.price is None for f in briefing.futures)
    assert briefing.volatility.band == "unknown"
    assert briefing.earnings == []
    assert briefing.themes  # fallback theme


def test_markdown_snapshot_renders(briefing) -> None:
    md = sunday_prep_markdown(briefing)
    assert md.startswith("## TRD Sunday Prep")
    assert "### 1. Futures Snapshot" in md
    assert "**Commodities**" in md
    assert "### 10. Weekly Mindset" in md
    assert briefing.prompt_question in md
