from datetime import date
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.errors import TrdError
from trd.models import InstrumentType
from trd.services import DcaProjectionService


def seed_monthly_growth_bars(
    conn: duckdb.DuckDBPyConnection,
    symbol: str,
    months: int,
    start_price: float,
    monthly_return: float,
) -> None:
    """One bar on the 1st of each month with exact multiplicative growth, so the
    derived monthly returns are assertable."""
    from trd.models import DailyBar, InstrumentInfo
    from trd.repos import InstrumentRepo, PriceRepo

    repo = InstrumentRepo(conn)
    instrument = repo.get_by_symbol(symbol) or repo.insert(
        InstrumentInfo(symbol=symbol, name=symbol, type=InstrumentType.ETF)
    )
    today = date.today()
    year, month = today.year, today.month
    # walk back `months` months
    for _ in range(months):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    bars = []
    price = start_price
    for _ in range(months + 1):
        value = Decimal(str(round(price, 6)))
        bars.append(
            DailyBar(
                date=date(year, month, 1),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=1_000_000,
                adj_close=value,
            )
        )
        price *= 1.0 + monthly_return
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    PriceRepo(conn).upsert_daily(instrument.id, bars)


@pytest.fixture
def projection(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> DcaProjectionService:
    provider.add_symbol("SPY", price="500.00", type_=InstrumentType.ETF)
    provider.add_symbol("QQQ", price="200.00", type_=InstrumentType.ETF)
    return DcaProjectionService(conn, provider)


def _plan(projection: DcaProjectionService, **kwargs) -> None:
    defaults: dict = {"monthly": Decimal(100), "ticker": "SPY"}
    defaults.update(kwargs)
    projection.plans.set_plan("sim", create_simulation=True, **defaults)


def test_derived_growth_matches_seeded(projection: DcaProjectionService) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=48, start_price=100.0, monthly_return=0.01
    )
    _plan(projection)
    returns, _, limiting = projection.portfolio_monthly_returns(projection.plans.get_plan("sim"))
    assert limiting == "SPY"
    assert len(returns) >= 47
    assert all(abs(r - 0.01) < 1e-6 for r in returns)


def test_deterministic_matches_closed_form(projection: DcaProjectionService) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=48, start_price=100.0, monthly_return=0.01
    )
    _plan(projection)
    result = projection.forecast("sim", years=2, trials=10, seed=1)
    g = result.monthly_growth
    assert abs(g - 0.01) < 1e-6
    fv_year1 = 100.0 * ((1 + g) ** 12 - 1) / g * (1 + g)  # v0 = 0
    assert abs(result.years[0].deterministic - fv_year1) < 0.01


def test_zero_variance_bands_collapse_to_deterministic(
    projection: DcaProjectionService,
) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=48, start_price=100.0, monthly_return=0.01
    )
    _plan(projection)
    result = projection.forecast("sim", years=3, trials=50, seed=7)
    for band in result.years:
        # every draw is the same 1% month → all percentiles equal the deterministic path
        assert abs(band.p10 - band.deterministic) < 0.01
        assert abs(band.p50 - band.deterministic) < 0.01
        assert abs(band.p90 - band.deterministic) < 0.01


def test_seeded_forecast_reproducible_and_ordered(projection: DcaProjectionService) -> None:
    # alternate +5%/-3% months → real variance
    from trd.models import DailyBar, InstrumentInfo
    from trd.repos import InstrumentRepo, PriceRepo

    repo = InstrumentRepo(projection.conn)
    instrument = repo.insert(InstrumentInfo(symbol="VAR", type=InstrumentType.ETF))
    today = date.today()
    year, month = today.year, today.month
    for _ in range(60):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    price = 100.0
    bars = []
    for i in range(61):
        value = Decimal(str(round(price, 6)))
        bars.append(
            DailyBar(
                date=date(year, month, 1),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=1,
                adj_close=value,
            )
        )
        price *= 1.05 if i % 2 == 0 else 0.97
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    PriceRepo(projection.conn).upsert_daily(instrument.id, bars)

    projection.plans.set_plan("sim", Decimal(100), create_simulation=True, ticker="VAR")
    a = projection.forecast("sim", years=5, trials=200, seed=42)
    b = projection.forecast("sim", years=5, trials=200, seed=42)
    assert [band.p50 for band in a.years] == [band.p50 for band in b.years]
    for band in a.years:
        assert band.p10 <= band.p50 <= band.p90


def test_window_limited_by_young_symbol(projection: DcaProjectionService) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=120, start_price=100.0, monthly_return=0.01
    )
    seed_monthly_growth_bars(
        projection.conn, "QQQ", months=30, start_price=100.0, monthly_return=0.01
    )
    _plan(projection, ticker=None, allocations={"SPY": Decimal(50), "QQQ": Decimal(50)})
    result = projection.forecast("sim", years=2, trials=10, seed=1)
    assert result.limiting_symbol == "QQQ"
    assert result.window_months <= 30


def test_too_little_history_raises(projection: DcaProjectionService) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=10, start_price=100.0, monthly_return=0.01
    )
    _plan(projection)
    with pytest.raises(TrdError, match="sync --years"):
        projection.forecast("sim", years=2)


def test_momentum_plan_rejected(projection: DcaProjectionService) -> None:
    _plan(projection, strategy="momentum", ticker=None)
    with pytest.raises(TrdError, match="Momentum"):
        projection.forecast("sim", years=2)


def test_backtest_hand_computable(projection: DcaProjectionService) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=36, start_price=100.0, monthly_return=0.0
    )
    _plan(projection, day_of_month=1)
    result = projection.backtest("sim", years=2)
    # flat prices: every $100 buys 1 share at 100 → value == invested, XIRR ≈ 0
    assert result.months >= 24
    assert result.invested == 100.0 * result.months
    assert abs(result.value - result.invested) < 0.01
    assert result.xirr is not None and abs(result.xirr) < 0.01
    assert abs(result.vs_spy) < 0.01  # plan IS SPY here
    assert result.skipped_months == 0


def test_backtest_writes_no_transactions(projection: DcaProjectionService) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=36, start_price=100.0, monthly_return=0.01
    )
    _plan(projection, day_of_month=1)
    before = projection.conn.execute("SELECT count(*) FROM txn").fetchone()
    projection.backtest("sim", years=2)
    after = projection.conn.execute("SELECT count(*) FROM txn").fetchone()
    assert before == after


def test_backtest_window_shortened_disclosed(projection: DcaProjectionService) -> None:
    seed_monthly_growth_bars(
        projection.conn, "SPY", months=30, start_price=100.0, monthly_return=0.01
    )
    _plan(projection, day_of_month=1)
    result = projection.backtest("sim", years=10)
    assert result.window_limited_by == "SPY"
    assert result.months <= 31
