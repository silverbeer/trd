from datetime import date, datetime, timedelta
from decimal import Decimal

from tests.conftest import seed_bars
from trd.models import Side
from trd.repos import InstrumentRepo, PriceRepo
from trd.services import PortfolioService
from trd.services.benchmark import BENCHMARK, same_dates_value


def test_benchmark_constant() -> None:
    assert BENCHMARK == "SPY"


def test_same_dates_value_accumulates_shares(portfolio: PortfolioService) -> None:
    conn = portfolio.conn
    # SPY flat at 100 across the window, latest also 100 → benchmark value == invested
    seed_bars(conn, "SPY", days=800, start_price=100.0, daily_gain=0.0)
    spy = InstrumentRepo(conn).get_by_symbol("SPY")
    assert spy is not None

    today = date.today()
    d1 = datetime.combine(today - timedelta(days=400), datetime.min.time())
    d2 = datetime.combine(today - timedelta(days=100), datetime.min.time())
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(10), Decimal(50), executed_at=d1)
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(5), Decimal(60), executed_at=d2)
    txns = portfolio.txns.list_chronological()

    value = same_dates_value(PriceRepo(conn), spy.id, txns)
    # invested = 500 + 300 = 800; SPY flat at 100 → 8 shares → worth 800
    assert value == Decimal(800)


def test_same_dates_value_empty(portfolio: PortfolioService) -> None:
    seed_bars(portfolio.conn, "SPY", days=10, start_price=100.0, daily_gain=0.0)
    spy = InstrumentRepo(portfolio.conn).get_by_symbol("SPY")
    assert spy is not None
    assert same_dates_value(PriceRepo(portfolio.conn), spy.id, []) is None
