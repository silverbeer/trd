from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider, months_ago
from tests.conftest import seed_bars as _seed_bars
from trd.errors import TrdError
from trd.models import AccountType, InstrumentType, Side
from trd.repos import AccountRepo
from trd.services import PlanService, PortfolioService, WatchlistService


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
        plans.invest("sim", when=months_ago(months_back))
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


def test_plan_note_round_trip(plans: PlanService) -> None:
    plans.set_plan(
        "sim",
        Decimal(100),
        create_simulation=True,
        note="QQQ tilt experiment: does 70/30 beat plain SPY over 12 months?",
    )
    plan = plans.get_plan("sim")
    assert plan.note is not None and "QQQ tilt" in plan.note


def test_day_of_month_round_trip_and_validation(plans: PlanService) -> None:
    _sim(plans, day_of_month=15)
    assert plans.get_plan("sim").day_of_month == 15
    with pytest.raises(TrdError, match="1-31"):
        plans.set_plan("sim2", Decimal(100), create_simulation=True, day_of_month=32)


def test_update_plan_partial(plans: PlanService) -> None:
    _sim(plans)
    plan = plans.update_plan("sim", day_of_month=15)
    assert plan.day_of_month == 15
    assert plan.monthly_amount == Decimal(100)  # untouched
    plan = plans.update_plan("sim", monthly=Decimal(250))
    assert plan.monthly_amount == Decimal(250)
    assert plan.day_of_month == 15  # untouched
    with pytest.raises(TrdError, match="Nothing to update"):
        plans.update_plan("sim")


def test_update_plan_reallocates(plans: PlanService) -> None:
    _sim(plans, allocations={"SPY": Decimal(40), "QQQ": Decimal(60)})
    plan = plans.update_plan("sim", allocations={"IVV": Decimal(25), "IXUS": Decimal(75)})
    assert plan.strategy == "allocation"
    assert plan.allocations == {"IXUS": Decimal(75), "IVV": Decimal(25)}
    assert "SPY" not in plan.allocations and "QQQ" not in plan.allocations


def test_update_plan_realloc_switches_ticker_strategy(plans: PlanService) -> None:
    _sim(plans, strategy="ticker", ticker="SPY")
    plan = plans.update_plan("sim", allocations={"IVV": Decimal(50), "QQQM": Decimal(50)})
    assert plan.strategy == "allocation"
    assert plan.strategy_ticker is None
    assert plan.allocations == {"IVV": Decimal(50), "QQQM": Decimal(50)}


def test_update_plan_realloc_weights_must_sum_100(plans: PlanService) -> None:
    _sim(plans)
    with pytest.raises(TrdError, match="sum to 100"):
        plans.update_plan("sim", allocations={"IVV": Decimal(40), "IXUS": Decimal(50)})


def test_pause_blocks_invest_resume_unblocks(plans: PlanService) -> None:
    _sim(plans)
    plans.pause("sim")
    assert plans.get_plan("sim").active is False
    with pytest.raises(TrdError, match="paused"):
        plans.invest("sim")
    plans.resume("sim")
    [txn] = plans.invest("sim")
    assert txn.quantity == Decimal("0.2")


def test_months_ago_is_month_arithmetic_not_thirty_day_steps() -> None:
    """The helper must give distinct calendar months on every possible today.

    30-day steps do not: from 2026-07-30 the 3- and 2-month offsets both land in
    May, and from 2026-03-31 the "1 month back" offset lands back inside March.
    Either one makes a plan reject a contribution, so these tests used to pass or
    fail depending on the date they ran — and broke first in UTC CI, where the
    date rolls over hours before it does locally.
    """
    start = date(2026, 1, 1)
    for offset in range(400):  # a year and a bit, every month length and leap edge
        today = start + timedelta(days=offset)
        picked = [months_ago(n, today=today) for n in (3, 2, 1)]
        months = {(d.year, d.month) for d in picked}
        assert len(months) == 3, f"{today}: {[str(d) for d in picked]}"
        assert all(d < today.replace(day=1) or d.month != today.month for d in picked)
        assert picked == sorted(picked)  # oldest first, as the callers assume
