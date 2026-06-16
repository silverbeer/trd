from decimal import Decimal

import duckdb

from trd.models import AccountType, Side
from trd.repos import AccountRepo
from trd.services import MoversService, PortfolioService, WatchlistService

from .conftest import FakeProvider


def _setup(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> None:
    AccountRepo(conn).create("main", AccountType.REAL)
    pf = PortfolioService(conn, provider)
    pf.record_trade("main", "AAPL", Side.BUY, Decimal("10"), price=Decimal("190"))
    wl = WatchlistService(conn, provider)
    wl.add("NVDA")  # watch-only
    wl.add("NVDA", "second")  # same symbol on a second list — must not duplicate
    wl.add("AAPL")  # owned AND watched


def test_board_merges_owned_and_watched(
    conn: duckdb.DuckDBPyConnection, provider: FakeProvider
) -> None:
    _setup(conn, provider)
    rows = MoversService(conn, provider).board()
    by = {r.symbol: r for r in rows}

    assert by["AAPL"].owned is True and by["AAPL"].watched is True
    assert by["AAPL"].pl == Decimal("100")  # 10 @ 190 cost, now 200
    assert by["AAPL"].day_change is not None
    assert by["NVDA"].owned is False and by["NVDA"].watched is True
    assert by["NVDA"].pl is None  # watch-only: no position, no P&L
    assert sum(1 for r in rows if r.symbol == "NVDA") == 1  # deduped across lists


def test_board_sorted_by_day_move(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> None:
    _setup(conn, provider)
    rows = MoversService(conn, provider).board(sort="day")
    # AAPL +2.56% (200 vs 195) ranks above NVDA -0.83% (120 vs 121)
    assert rows[0].symbol == "AAPL"
    assert rows[-1].symbol == "NVDA"


def test_board_sort_by_symbol(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> None:
    _setup(conn, provider)
    rows = MoversService(conn, provider).board(sort="symbol")
    assert [r.symbol for r in rows] == sorted(r.symbol for r in rows)
