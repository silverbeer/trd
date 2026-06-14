from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from trd.errors import TrdError
from trd.models import AccountType, DailyBar, InstrumentInfo, InstrumentType, Side
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo, TransactionRepo
from trd.services import EquityCurveService

TODAY = date.today()


def _seed_prices(
    conn: duckdb.DuckDBPyConnection, symbol: str, start: date, closes: list[float]
) -> int:
    instruments = InstrumentRepo(conn)
    inst = instruments.get_by_symbol(symbol) or instruments.insert(
        InstrumentInfo(symbol=symbol, name=symbol, type=InstrumentType.ETF)
    )
    bars = [
        DailyBar(
            date=start + timedelta(days=i),
            open=Decimal(str(v)),
            high=Decimal(str(v)),
            low=Decimal(str(v)),
            close=Decimal(str(v)),
            adj_close=Decimal(str(v)),
        )
        for i, v in enumerate(closes)
    ]
    PriceRepo(conn).upsert_daily(inst.id, bars)
    return inst.id


def _account(conn: duckdb.DuckDBPyConnection, name: str = "main") -> int:
    return AccountRepo(conn).create(name, AccountType.REAL).id


def _buy(
    conn: duckdb.DuckDBPyConnection,
    account_id: int,
    instrument_id: int,
    qty: str,
    price: str,
    when: date,
    side: Side = Side.BUY,
    fees: str = "0",
) -> None:
    TransactionRepo(conn).insert(
        account_id=account_id,
        instrument_id=instrument_id,
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fees=Decimal(fees),
        executed_at=datetime(when.year, when.month, when.day),
    )


def test_rising_curve_values_and_return(conn: duckdb.DuckDBPyConnection) -> None:
    acct = _account(conn)
    start = TODAY - timedelta(days=10)
    iid = _seed_prices(conn, "AAPL", start, [100 + i for i in range(11)])  # 100..110
    _buy(conn, acct, iid, "10", "100", start)

    curve = EquityCurveService(conn).curve()
    assert curve.points
    assert [p.date for p in curve.points] == sorted(p.date for p in curve.points)
    assert curve.start_value == Decimal("1000")  # 10 * 100
    assert curve.end_value == Decimal("1100")  # 10 * 110
    assert curve.period_return_pct == pytest.approx(10.0)
    assert all(p.cost_basis == Decimal("1000") for p in curve.points)
    assert curve.max_drawdown_pct == 0.0  # monotonic rise
    assert curve.pl_pct == pytest.approx(10.0)


def test_sell_reduces_holdings_midcurve(conn: duckdb.DuckDBPyConnection) -> None:
    acct = _account(conn)
    start = TODAY - timedelta(days=6)
    iid = _seed_prices(conn, "AAPL", start, [100, 100, 100, 100, 100, 100, 100])
    _buy(conn, acct, iid, "10", "100", start)
    _buy(conn, acct, iid, "5", "100", start + timedelta(days=3), side=Side.SELL)

    curve = EquityCurveService(conn).curve()
    before = next(p for p in curve.points if p.date == start)
    after = next(p for p in curve.points if p.date == start + timedelta(days=4))
    assert before.value == Decimal("1000")  # 10 * 100
    assert after.value == Decimal("500")  # 5 * 100 after the sell
    assert after.cost_basis == Decimal("500")  # FIFO: 5 shares of the original lot remain


def test_max_drawdown(conn: duckdb.DuckDBPyConnection) -> None:
    acct = _account(conn)
    start = TODAY - timedelta(days=4)
    _seed = _seed_prices(conn, "AAPL", start, [100, 110, 120, 90, 95])  # peak 1200, trough 900
    _buy(conn, acct, _seed, "10", "100", start)

    curve = EquityCurveService(conn).curve()
    assert curve.max_drawdown_pct == pytest.approx(-25.0)  # (900 - 1200) / 1200


def test_xirr_present_for_long_rising_series(conn: duckdb.DuckDBPyConnection) -> None:
    acct = _account(conn)
    start = TODAY - timedelta(days=40)
    iid = _seed_prices(conn, "AAPL", start, [100 + i for i in range(41)])
    _buy(conn, acct, iid, "10", "100", start)

    curve = EquityCurveService(conn).curve()
    assert curve.xirr is not None
    assert curve.xirr > 0


def test_no_transactions_raises(conn: duckdb.DuckDBPyConnection) -> None:
    _account(conn)
    with pytest.raises(TrdError):
        EquityCurveService(conn).curve()


def test_account_scoping(conn: duckdb.DuckDBPyConnection) -> None:
    main = _account(conn, "main")
    roth = _account(conn, "roth")
    start = TODAY - timedelta(days=5)
    iid = _seed_prices(conn, "AAPL", start, [100, 100, 100, 100, 100, 100])
    _buy(conn, main, iid, "10", "100", start)
    _buy(conn, roth, iid, "3", "100", start)

    all_curve = EquityCurveService(conn).curve()
    main_curve = EquityCurveService(conn).curve(account_name="main")
    assert all_curve.end_value == Decimal("1300")  # 13 shares
    assert main_curve.end_value == Decimal("1000")  # 10 shares


def test_lookback_window_trims_start(conn: duckdb.DuckDBPyConnection) -> None:
    acct = _account(conn)
    start = TODAY - timedelta(days=20)
    iid = _seed_prices(conn, "AAPL", start, [100 + i for i in range(21)])
    _buy(conn, acct, iid, "10", "100", start)

    full = EquityCurveService(conn).curve()
    windowed = EquityCurveService(conn).curve(lookback_days=5)
    assert windowed.start_date > full.start_date
    assert windowed.start_date >= TODAY - timedelta(days=5)


def test_unpriced_symbol_excluded(conn: duckdb.DuckDBPyConnection) -> None:
    acct = _account(conn)
    start = TODAY - timedelta(days=5)
    priced = _seed_prices(conn, "AAPL", start, [100, 100, 100, 100, 100, 100])
    # An instrument with no price history at all.
    unpriced_inst = InstrumentRepo(conn).insert(
        InstrumentInfo(symbol="ZZZZ", name="ZZZZ", type=InstrumentType.STOCK)
    )
    _buy(conn, acct, priced, "10", "100", start)
    _buy(conn, acct, unpriced_inst.id, "5", "50", start)

    curve = EquityCurveService(conn).curve()
    assert "ZZZZ" in curve.unpriced
    assert curve.end_value == Decimal("1000")  # only the priced position counts
