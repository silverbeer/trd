from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider, seed_bars
from trd.models import AccountType, InstrumentType, Side
from trd.services import DashboardService, PortfolioService


@pytest.fixture
def dash_service(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> DashboardService:
    # winners and losers, plus SPY for the benchmark
    provider.add_symbol("WIN", price="200.00", prev_close="198.00", type_=InstrumentType.STOCK)
    provider.add_symbol("LOSE", price="50.00", prev_close="51.00", type_=InstrumentType.STOCK)
    provider.add_symbol("SPY", price="100.00", prev_close="99.50", type_=InstrumentType.ETF)
    return DashboardService(conn, provider)


def _seed_portfolio(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> None:
    portfolio = PortfolioService(conn, provider)
    portfolio.accounts.create("main", AccountType.REAL)
    portfolio.accounts.create("fidelity", AccountType.REAL)
    portfolio.accounts.create("paper", AccountType.SIMULATION)
    seed_bars(conn, "SPY", days=800, start_price=100.0, daily_gain=0.0)
    old = datetime.combine(date.today() - timedelta(days=400), datetime.min.time())
    # WIN: paid 100, now 200 (+100%). LOSE: paid 100, now 50 (-50%).
    portfolio.record_trade("main", "WIN", Side.BUY, Decimal(10), Decimal(100), executed_at=old)
    portfolio.record_trade("fidelity", "LOSE", Side.BUY, Decimal(10), Decimal(100), executed_at=old)
    # paper account holding — must be excluded by default
    portfolio.record_trade("paper", "WIN", Side.BUY, Decimal(100), Decimal(100), executed_at=old)


def test_aggregates_real_accounts_only(dash_service: DashboardService, provider) -> None:
    _seed_portfolio(dash_service.conn, provider)
    dash = dash_service.summary()
    # WIN 10@200=2000, LOSE 10@50=500 → value 2500; paper WIN excluded
    assert dash.value == Decimal(2500)
    assert dash.invested == Decimal(2000)  # 1000 + 1000
    assert dash.gains == Decimal(500)  # +1000 WIN, -500 LOSE
    assert dash.total_return_pct == Decimal(25)


def test_include_simulation_flag(dash_service: DashboardService, provider) -> None:
    _seed_portfolio(dash_service.conn, provider)
    dash_all = dash_service.summary(include_simulation=True)
    # paper adds 100 WIN @ 200 = 20000 to value
    assert dash_all.value == Decimal(22500)


def test_allocation_and_concentration(dash_service: DashboardService, provider) -> None:
    _seed_portfolio(dash_service.conn, provider)
    dash = dash_service.summary()
    assert dash.holdings[0].symbol == "WIN"  # biggest value
    assert dash.holdings[0].weight == Decimal(80)  # 2000/2500
    assert dash.concentration_warning is True  # 80% >= 25%
    assert abs(dash.top5_weight - Decimal(100)) < Decimal("0.01")


def test_movers_and_win_rate(dash_service: DashboardService, provider) -> None:
    _seed_portfolio(dash_service.conn, provider)
    dash = dash_service.summary()
    assert dash.winners[0].symbol == "WIN"
    assert dash.losers[0].symbol == "LOSE"
    assert dash.positions_up == 1
    assert dash.positions_down == 1
    assert dash.win_rate == Decimal(50)


def test_xirr_and_benchmark_present(dash_service: DashboardService, provider) -> None:
    _seed_portfolio(dash_service.conn, provider)
    dash = dash_service.summary()
    assert dash.xirr is not None  # has buys + terminal value over >30 days
    # SPY flat at 100, invested 2000 → benchmark 20 shares → worth 2000 → return 0%
    assert dash.benchmark_value == Decimal(2000)
    assert dash.benchmark_return_pct == Decimal(0)
    # portfolio +25% vs SPY 0% → alpha +25pp
    assert dash.alpha == Decimal(25)


def test_today_change(dash_service: DashboardService, provider) -> None:
    _seed_portfolio(dash_service.conn, provider)
    dash = dash_service.summary()
    # WIN +2 * 10 = +20, LOSE -1 * 10 = -10 → +10
    assert dash.today_change == Decimal(10)
    assert dash.spy_today_pct is not None


def test_empty_portfolio(dash_service: DashboardService) -> None:
    dash = dash_service.summary()
    assert dash.value == Decimal(0)
    assert dash.holdings == []
    assert dash.xirr is None
    assert dash.total_return_pct is None
