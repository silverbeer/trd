"""Transaction history and the realized P&L attached to every sell."""

from datetime import datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.errors import TrdError
from trd.models import AccountType, InstrumentInfo, Side, Transaction
from trd.repos import AccountRepo, InstrumentRepo, TransactionRepo
from trd.services import HistoryService
from trd.services.fifo import realized_sales


def _txn(id_: int, side: Side, qty: str, price: str, day: datetime, fees: str = "0"):
    return Transaction(
        id=id_,
        account_id=1,
        instrument_id=1,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fees=Decimal(fees),
        executed_at=day,
    )


# --------------------------------------------------------------------- fifo


def test_realized_sale_matches_the_lots_it_consumed():
    day = datetime(2026, 1, 1)
    txns = [
        _txn(1, Side.BUY, "10", "100", day),
        _txn(2, Side.SELL, "10", "150", day + timedelta(days=1)),
    ]
    sale = realized_sales(txns)[2]
    assert sale.proceeds == Decimal("1500")
    assert sale.cost == Decimal("1000")
    assert sale.pnl == Decimal("500")
    assert sale.pnl_pct == Decimal("50")


def test_realized_sale_consumes_oldest_lots_first():
    """FIFO, so a sale after a cheap lot and an expensive one takes the cheap one."""
    day = datetime(2026, 1, 1)
    txns = [
        _txn(1, Side.BUY, "10", "100", day),
        _txn(2, Side.BUY, "10", "200", day + timedelta(days=1)),
        _txn(3, Side.SELL, "10", "300", day + timedelta(days=2)),
    ]
    sale = realized_sales(txns)[3]
    assert sale.cost == Decimal("1000")  # the 100 lot, not the 200 one
    assert sale.pnl == Decimal("2000")


def test_realized_sale_splits_a_partly_consumed_lot():
    day = datetime(2026, 1, 1)
    txns = [
        _txn(1, Side.BUY, "10", "100", day),
        _txn(2, Side.SELL, "4", "150", day + timedelta(days=1)),
        _txn(3, Side.SELL, "6", "200", day + timedelta(days=2)),
    ]
    sales = realized_sales(txns)
    assert sales[2].cost == Decimal("400")  # 4 of the 10 shares
    assert sales[3].cost == Decimal("600")  # the remaining 6
    # 600 + 1200 proceeds against the lot's 1000 cost.
    assert sales[2].pnl + sales[3].pnl == Decimal("800")


def test_fees_reduce_proceeds_and_raise_cost():
    day = datetime(2026, 1, 1)
    txns = [
        _txn(1, Side.BUY, "10", "100", day, fees="5"),
        _txn(2, Side.SELL, "10", "100", day + timedelta(days=1), fees="5"),
    ]
    sale = realized_sales(txns)[2]
    assert sale.proceeds == Decimal("995")
    assert sale.cost == Decimal("1005")
    assert sale.pnl == Decimal("-10")  # a flat round trip still loses both fees


# ------------------------------------------------------------------ service


@pytest.fixture
def history(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> HistoryService:
    AccountRepo(conn).create("main", AccountType.REAL)
    AccountRepo(conn).create("paper", AccountType.SIMULATION)
    InstrumentRepo(conn).insert(InstrumentInfo(symbol="AAA", name="AAA"))
    return HistoryService(conn)


def _record(conn, account_id: int, side: Side, qty: str, price: str, when: datetime) -> None:
    TransactionRepo(conn).insert(
        account_id=account_id,
        instrument_id=1,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fees=Decimal(0),
        executed_at=when,
    )


def test_realized_pnl_survives_the_date_window(history, conn):
    """The invariant that matters: FIFO matches over the whole history, then the
    window filters what is shown. Matching after filtering would price the sale
    against lots that were never bought, and invent a profit."""
    long_ago = datetime.now() - timedelta(days=200)
    yesterday = datetime.now() - timedelta(days=1)
    _record(conn, 1, Side.BUY, "10", "100", long_ago)
    _record(conn, 1, Side.SELL, "10", "150", yesterday)

    result = history.history(days=7)
    assert len(result.rows) == 1  # only the sell is inside the window
    assert result.rows[0].realized_pnl == Decimal("500")  # priced against the old buy
    assert result.realized_pnl == Decimal("500")
    assert result.bought == Decimal(0)
    assert result.sold == Decimal("1500")


def test_paper_accounts_are_excluded_by_default(history, conn):
    """The engine makes several paper fills a day; left in they bury the trades
    actually worth reviewing."""
    now = datetime.now()
    _record(conn, 1, Side.BUY, "1", "100", now)
    _record(conn, 2, Side.BUY, "1", "100", now)

    assert len(history.history(days=7).rows) == 1
    assert len(history.history(days=7, include_simulation=True).rows) == 2


def test_filters_narrow_to_one_account_side_or_symbol(history, conn):
    now = datetime.now()
    _record(conn, 1, Side.BUY, "10", "100", now - timedelta(days=2))
    _record(conn, 1, Side.SELL, "5", "120", now)

    assert len(history.history(days=7, side=Side.SELL).rows) == 1
    assert len(history.history(days=7, side=Side.BUY).rows) == 1
    assert len(history.history(days=7, symbol="aaa").rows) == 2
    assert len(history.history(days=7, account="main").rows) == 2
    with pytest.raises(TrdError, match="No account named"):
        history.history(account="nope")
    with pytest.raises(TrdError, match="Unknown symbol"):
        history.history(symbol="ZZZZ")


def test_rows_are_newest_first_and_totals_add_up(history, conn):
    now = datetime.now()
    _record(conn, 1, Side.BUY, "10", "100", now - timedelta(days=3))
    _record(conn, 1, Side.SELL, "10", "130", now - timedelta(days=1))

    result = history.history(days=7)
    assert [r.txn.side for r in result.rows] == [Side.SELL, Side.BUY]
    assert result.bought == Decimal("1000")
    assert result.sold == Decimal("1300")
    assert result.realized_pnl == Decimal("300")
    assert result.net_invested == Decimal("-300")  # more came back than went out
    assert result.realized_pct == Decimal("30")
    assert result.sells_with_result == 1


def test_a_period_with_no_sells_reports_no_realized_result(history, conn):
    """Distinct from breaking even — 'realized 0.00' would read as a wash."""
    _record(conn, 1, Side.BUY, "10", "100", datetime.now())
    result = history.history(days=7)
    assert result.sells_with_result == 0
    assert result.realized_pnl == Decimal(0)
    assert result.realized_pct is None


def test_all_time_ignores_the_window(history, conn):
    _record(conn, 1, Side.BUY, "1", "100", datetime.now() - timedelta(days=900))
    assert history.history(days=30).rows == []
    assert len(history.history(days=None).rows) == 1


def test_empty_window_still_points_at_the_data(history, conn):
    """An empty window and an empty database must not look the same: the first
    means look further back, the second means the tool has nothing at all."""
    _record(conn, 1, Side.BUY, "1", "100", datetime.now() - timedelta(days=100))

    recent = history.history(days=30)
    assert recent.rows == []
    assert recent.latest_outside_window is not None  # there IS history

    wide = history.history(days=None)
    assert len(wide.rows) == 1


def test_a_genuinely_empty_database_says_nothing_exists(history):
    result = history.history(days=30)
    assert result.rows == []
    assert result.latest_outside_window is None


def test_latest_outside_window_respects_the_other_filters(history, conn):
    """Pointing at a transaction the filters would have excluded anyway would
    send someone widening a window that was never going to show it."""
    _record(conn, 1, Side.BUY, "1", "100", datetime.now() - timedelta(days=100))
    assert history.history(days=30, side=Side.BUY).latest_outside_window is not None
    assert history.history(days=30, side=Side.SELL).latest_outside_window is None
