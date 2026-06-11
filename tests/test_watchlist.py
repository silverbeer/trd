from datetime import date, timedelta
from decimal import Decimal

import pytest

from trd.errors import TrdError
from trd.models import EarningsDate
from trd.repos import EarningsRepo
from trd.services import WatchlistService


def test_add_creates_list_and_instrument(watchlist: WatchlistService) -> None:
    assert watchlist.add("nvda", "ai") is True
    assert watchlist.instruments.get_by_symbol("NVDA") is not None
    assert watchlist.watchlists.get_by_name("ai") is not None


def test_add_duplicate_returns_false(watchlist: WatchlistService) -> None:
    watchlist.add("NVDA")
    assert watchlist.add("NVDA") is False


def test_remove(watchlist: WatchlistService) -> None:
    watchlist.add("NVDA")
    watchlist.remove("NVDA")
    assert watchlist.board() == []


def test_remove_not_watched_raises(watchlist: WatchlistService) -> None:
    with pytest.raises(TrdError, match="not on watchlist"):
        watchlist.remove("NVDA")


def test_board_scoped_to_list(watchlist: WatchlistService) -> None:
    watchlist.add("NVDA", "ai")
    watchlist.add("AAPL", "mega")
    rows = watchlist.board("ai")
    assert [r.instrument.symbol for r in rows] == ["NVDA"]
    assert len(watchlist.board()) == 2


def test_board_unknown_list_raises(watchlist: WatchlistService) -> None:
    with pytest.raises(TrdError, match="No watchlist named"):
        watchlist.board("nope")


def test_board_metrics(watchlist: WatchlistService, provider) -> None:
    provider.add_symbol(
        "HOT",
        price="90",
        prev_close="80",
        year_high="100",
        year_low="50",
        volume=3_000_000,
        avg_volume=1_000_000,
    )
    watchlist.add("HOT")
    [row] = watchlist.board()
    assert row.quote is not None
    assert row.quote.year_range_pct == Decimal(80)  # (90-50)/(100-50)
    assert row.quote.volume_ratio == Decimal(3)
    assert not row.price_stale


def test_board_shows_next_earnings(watchlist: WatchlistService, provider) -> None:
    watchlist.add("NVDA")
    instrument = watchlist.instruments.get_by_symbol("NVDA")
    assert instrument is not None
    soon = date.today() + timedelta(days=5)
    EarningsRepo(watchlist.conn).upsert(
        instrument.id,
        [
            EarningsDate(date=date.today() - timedelta(days=90)),  # past — ignored
            EarningsDate(date=soon, eps_estimate=Decimal("1.25")),
        ],
    )
    [row] = watchlist.board()
    assert row.next_earnings == soon


def test_board_stale_fallback(watchlist: WatchlistService, provider) -> None:
    watchlist.add("NVDA")
    watchlist.board()  # snapshot stored
    provider.drop_quote("NVDA")
    [row] = watchlist.board()
    assert row.price_stale
    assert row.quote is not None
    assert row.quote.price == Decimal("120.00")
