from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.errors import TrdError
from trd.models import AccountType, DailyBar, InstrumentInfo, InstrumentType, Side
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo
from trd.services import PlanService, PortfolioService, WatchlistService


def _seed_bars(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    days: int,
    start_price: float,
    daily_gain: float,
) -> None:
    repo = InstrumentRepo(conn)
    instrument = repo.get_by_symbol(symbol) or repo.insert(
        InstrumentInfo(symbol=symbol, name=symbol, type=InstrumentType.ETF)
    )
    today = date.today()
    bars = []
    price = start_price
    for i in range(days):
        bars.append(
            DailyBar(
                date=today - timedelta(days=days - i),
                open=Decimal(str(round(price, 4))),
                high=Decimal(str(round(price * 1.01, 4))),
                low=Decimal(str(round(price * 0.99, 4))),
                close=Decimal(str(round(price, 4))),
                volume=1_000_000,
            )
        )
        price += daily_gain
    PriceRepo(conn).upsert_daily(instrument.id, bars)


@pytest.fixture
def plans(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> PlanService:
    provider.add_symbol("SPY", price="500.00", prev_close="495.00", type_=InstrumentType.ETF)
    return PlanService(conn, provider)


def _sim(plans: PlanService, **kwargs) -> None:
    defaults: dict = {"monthly": Decimal(100)}
    defaults.update(kwargs)
    plans.set_plan("sim", create_simulation=True, **defaults)


def test_sim_init_creates_simulation_account(plans: PlanService) -> None:
    _sim(plans)
    plan = plans.get_plan("sim")
    assert plan.account.type == "simulation"
    assert plan.is_paper
    assert plan.monthly_amount == Decimal(100)
    assert plan.strategy_ticker == "SPY"


def test_plan_on_unknown_real_account_rejected(plans: PlanService) -> None:
    with pytest.raises(TrdError, match="No account named"):
        plans.set_plan("sofi", Decimal(100))


def test_plan_on_real_account(plans: PlanService) -> None:
    AccountRepo(plans.conn).create("sofi", AccountType.REAL)
    plan = plans.set_plan("sofi", Decimal(100))
    assert not plan.is_paper


def test_plan_twice_rejected(plans: PlanService) -> None:
    _sim(plans)
    with pytest.raises(TrdError, match="already has a plan"):
        plans.set_plan("sim", Decimal(100))


def test_plan_validates(plans: PlanService) -> None:
    with pytest.raises(TrdError, match="positive"):
        _sim(plans, monthly=Decimal(0))
    with pytest.raises(TrdError, match="'momentum', or 'allocation'"):
        _sim(plans, strategy="vibes")
    with pytest.raises(TrdError, match="sum to 100"):
        _sim(plans, allocations={"SPY": Decimal(30), "QQQ": Decimal(60)})
    with pytest.raises(TrdError, match="needs --alloc"):
        _sim(plans, strategy="allocation")


def test_invest_buys_fraction_at_live_price(plans: PlanService) -> None:
    _sim(plans)
    [txn] = plans.invest("sim")
    assert txn.price == Decimal("500.00")
    assert txn.quantity == Decimal("0.2")
    assert txn.plan_id == plans.get_plan("sim").id


def test_invest_twice_same_month_rejected(plans: PlanService) -> None:
    _sim(plans)
    plans.invest("sim")
    with pytest.raises(TrdError, match="already invested"):
        plans.invest("sim")


def test_backdated_invest_uses_historical_close(plans: PlanService) -> None:
    _sim(plans)
    _seed_bars(plans.conn, "SPY", days=400, start_price=400.0, daily_gain=0.25)
    when = date.today() - timedelta(days=90)
    [txn] = plans.invest("sim", when=when)
    assert txn.executed_at.date() >= when - timedelta(days=1)
    assert txn.price != Decimal("500.00")  # historical close, not live quote
    [txn2] = plans.invest("sim")
    assert txn2.price == Decimal("500.00")


def test_status_math_and_benchmark(plans: PlanService) -> None:
    _sim(plans)
    _seed_bars(plans.conn, "SPY", days=400, start_price=400.0, daily_gain=0.25)
    for months_back in (3, 2, 1):
        plans.invest("sim", when=date.today() - timedelta(days=30 * months_back))
    status = plans.status("sim")
    assert status.months_invested == 3
    assert Decimal(299) < status.invested < Decimal(301)
    assert status.value is not None
    assert status.benchmark_value is not None
    assert abs(status.value - status.benchmark_value) < Decimal("0.01") * status.benchmark_value


def test_real_account_plan_isolated_from_other_holdings(
    plans: PlanService, provider: FakeProvider
) -> None:
    """The headline feature: plan status scores only plan-tagged txns, even when
    the same real account holds other positions."""
    AccountRepo(plans.conn).create("sofi", AccountType.REAL)
    portfolio = PortfolioService(plans.conn, provider)
    # unrelated pre-existing holding in the same account
    portfolio.record_trade("sofi", "NVDA", Side.BUY, Decimal(10), Decimal(100))

    plans.set_plan("sofi", Decimal(100))
    [txn] = plans.invest("sofi")
    assert txn.plan_id is not None

    status = plans.status("sofi")
    assert status.invested == Decimal("100.00")  # NVDA's $1000 not counted
    assert status.value is not None
    assert status.value < Decimal(200)  # plan value only, not the NVDA position

    # account-level portfolio still sees everything
    symbols = {p.instrument.symbol for p in portfolio.positions("sofi")}
    assert symbols == {"NVDA", "SPY"}


def test_allocation_invest_splits_monthly(plans: PlanService, provider: FakeProvider) -> None:
    provider.add_symbol("QQQ", price="200.00", type_=InstrumentType.ETF)
    _sim(plans, allocations={"SPY": Decimal(30), "QQQ": Decimal(70)})
    plan = plans.get_plan("sim")
    assert plan.strategy == "allocation"
    assert "30% SPY" in plan.strategy_label and "70% QQQ" in plan.strategy_label

    txns = plans.invest("sim")
    assert len(txns) == 2
    by_symbol = {}
    for txn in txns:
        instrument = plans.instruments.get(txn.instrument_id)
        assert instrument is not None
        by_symbol[instrument.symbol] = txn
    assert by_symbol["SPY"].quantity == Decimal("0.06")  # $30 @ 500
    assert by_symbol["QQQ"].quantity == Decimal("0.35")  # $70 @ 200

    with pytest.raises(TrdError, match="already invested"):
        plans.invest("sim")


def test_status_counts_months_not_txns(plans: PlanService, provider: FakeProvider) -> None:
    provider.add_symbol("QQQ", price="200.00", type_=InstrumentType.ETF)
    _sim(plans, allocations={"SPY": Decimal(50), "QQQ": Decimal(50)})
    _seed_bars(plans.conn, "SPY", days=400, start_price=400.0, daily_gain=0.25)
    _seed_bars(plans.conn, "QQQ", days=400, start_price=150.0, daily_gain=0.10)
    plans.invest("sim", when=date.today() - timedelta(days=60))
    plans.invest("sim")
    status = plans.status("sim")
    assert status.months_invested == 2  # 4 txns, 2 months
    assert Decimal(199) < status.invested < Decimal(201)


def test_momentum_picks_strongest(plans: PlanService, provider: FakeProvider) -> None:
    provider.add_symbol("HOT", price="150.00", type_=InstrumentType.STOCK)
    provider.add_symbol("COLD", price="50.00", type_=InstrumentType.STOCK)
    watch = WatchlistService(plans.conn, provider)
    watch.add("HOT")
    watch.add("COLD")
    _seed_bars(plans.conn, "HOT", days=100, start_price=100.0, daily_gain=0.5)
    _seed_bars(plans.conn, "COLD", days=100, start_price=100.0, daily_gain=-0.4)
    _sim(plans, strategy="momentum", ticker=None)
    [txn] = plans.invest("sim")
    hot = plans.instruments.get_by_symbol("HOT")
    assert hot is not None and txn.instrument_id == hot.id


def test_momentum_needs_watchlist(plans: PlanService) -> None:
    _sim(plans, strategy="momentum", ticker=None)
    with pytest.raises(TrdError, match="watchlist"):
        plans.invest("sim")


def test_resolve_default_account(plans: PlanService) -> None:
    with pytest.raises(TrdError, match="No plans yet"):
        plans.resolve_default_account()
    _sim(plans)
    assert plans.resolve_default_account() == "sim"
    AccountRepo(plans.conn).create("sofi", AccountType.REAL)
    plans.set_plan("sofi", Decimal(50))
    with pytest.raises(TrdError, match="Multiple plans"):
        plans.resolve_default_account()
    assert len(plans.list_plans()) == 2


def test_status_without_plan_raises(plans: PlanService) -> None:
    with pytest.raises(TrdError, match="No plan on account"):
        plans.status("sim")
