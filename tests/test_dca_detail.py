from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider, seed_bars
from trd.models import InstrumentType
from trd.services import DcaDetailService, PlanService


@pytest.fixture
def detail_service(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> DcaDetailService:
    provider.add_symbol("SPY", price="500.00", prev_close="495.00", type_=InstrumentType.ETF)
    provider.add_symbol("QQQ", price="200.00", type_=InstrumentType.ETF)
    seed_bars(conn, "SPY", days=500, start_price=500.0, daily_gain=0.0)
    seed_bars(conn, "QQQ", days=500, start_price=200.0, daily_gain=0.0)
    return DcaDetailService(conn, provider)


def _make_plan(service: DcaDetailService, **kwargs) -> PlanService:
    plans = service.plans
    defaults: dict = {
        "monthly": Decimal(100),
        "allocations": {"SPY": Decimal(50), "QQQ": Decimal(50)},
        "day_of_month": 15,
    }
    defaults.update(kwargs)
    plans.set_plan("sim", create_simulation=True, **defaults)
    return plans


def test_events_group_legs_by_date(detail_service: DcaDetailService) -> None:
    plans = _make_plan(detail_service)
    plans.invest("sim")
    detail = detail_service.detail("sim")
    assert len(detail.events) == 1
    event = detail.events[0]
    assert len(event.legs) == 2  # 50/50 allocation = 2 legs in one event
    assert event.total == Decimal("100.00")
    assert {leg.symbol for leg in event.legs} == {"SPY", "QQQ"}


def test_symbol_stats_weights_and_drift(detail_service: DcaDetailService) -> None:
    plans = _make_plan(detail_service)
    plans.invest("sim")
    detail = detail_service.detail("sim")
    by_symbol = {s.symbol: s for s in detail.symbol_stats}
    spy = by_symbol["SPY"]
    assert spy.invested == Decimal("50.00")
    assert spy.quantity == Decimal("0.1")  # $50 @ 500
    assert spy.avg_cost == Decimal("500")
    assert spy.target_weight == Decimal(50)
    # equal invest, equal flat prices → actual ≈ 50, drift ≈ 0
    assert spy.actual_weight is not None and abs(spy.actual_weight - 50) < Decimal("0.01")
    assert spy.drift is not None and abs(spy.drift) < Decimal("0.01")


def test_cadence_missed_month_and_streak(detail_service: DcaDetailService) -> None:
    plans = _make_plan(detail_service)
    today = date.today()
    # invest 3 months ago and 1 month ago on the 15th; skip 2 months ago
    for months_back in (3, 1):
        target = today - timedelta(days=30 * months_back)
        plans.invest("sim", when=target.replace(day=15))
    detail = detail_service.detail("sim")
    cadence = detail.cadence
    assert cadence.months_invested == 2
    assert cadence.missed >= 1  # the skipped month (current month may add another due)
    assert cadence.streak >= 1
    assert cadence.next_due is not None
    assert cadence.next_due.day in (15, 28, 29, 30, 31)


def test_xirr_present_and_plausible(detail_service: DcaDetailService) -> None:
    plans = _make_plan(detail_service)
    today = date.today()
    for months_back in (14, 8, 2):
        target = today - timedelta(days=30 * months_back)
        plans.invest("sim", when=target.replace(day=15))
    detail = detail_service.detail("sim")
    # flat fake prices → contributions worth exactly what was paid → XIRR ≈ 0
    assert detail.xirr is not None
    assert abs(detail.xirr) < 0.01


def test_detail_empty_plan(detail_service: DcaDetailService) -> None:
    _make_plan(detail_service)
    detail = detail_service.detail("sim")
    assert detail.events == []
    assert detail.symbol_stats == []
    assert detail.cadence.months_invested == 0
    assert detail.xirr is None
