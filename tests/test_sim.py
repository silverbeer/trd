from datetime import date, timedelta
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.errors import TrdError
from trd.models import DailyBar, InstrumentInfo, InstrumentType
from trd.repos import InstrumentRepo, PriceRepo
from trd.services import SimService, WatchlistService


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
def sim(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> SimService:
    provider.add_symbol("SPY", price="500.00", prev_close="495.00", type_=InstrumentType.ETF)
    return SimService(conn, provider)


def test_init_creates_simulation_account(sim: SimService) -> None:
    config = sim.init(Decimal(100))
    assert config.account.type == "simulation"
    assert config.monthly_amount == Decimal(100)
    assert config.strategy_ticker == "SPY"


def test_init_twice_rejected(sim: SimService) -> None:
    sim.init(Decimal(100))
    with pytest.raises(TrdError, match="already exists"):
        sim.init(Decimal(100))


def test_init_validates(sim: SimService) -> None:
    with pytest.raises(TrdError, match="positive"):
        sim.init(Decimal(0))
    with pytest.raises(TrdError, match="ticker' or 'momentum"):
        sim.init(Decimal(100), strategy="vibes")


def test_invest_buys_fraction_at_live_price(sim: SimService) -> None:
    sim.init(Decimal(100))
    txn, symbol = sim.invest()
    assert symbol == "SPY"
    assert txn.price == Decimal("500.00")
    assert txn.quantity == Decimal("0.2")


def test_invest_twice_same_month_rejected(sim: SimService) -> None:
    sim.init(Decimal(100))
    sim.invest()
    with pytest.raises(TrdError, match="Already invested"):
        sim.invest()


def test_backdated_invest_uses_historical_close(sim: SimService) -> None:
    sim.init(Decimal(100))
    _seed_bars(sim.conn, "SPY", days=400, start_price=400.0, daily_gain=0.25)
    when = date.today() - timedelta(days=90)
    txn, _ = sim.invest(when=when)
    assert txn.executed_at.date() >= when - timedelta(days=1)
    assert txn.price != Decimal("500.00")  # historical close, not live quote
    # backdated month and current month are independent
    txn2, _ = sim.invest()
    assert txn2.price == Decimal("500.00")


def test_status_math_and_benchmark(sim: SimService) -> None:
    sim.init(Decimal(100))
    _seed_bars(sim.conn, "SPY", days=400, start_price=400.0, daily_gain=0.25)
    for months_back in (3, 2, 1):
        target = date.today() - timedelta(days=30 * months_back)
        sim.invest(when=target)
    status = sim.status()
    assert status.months_invested == 3
    assert Decimal(299) < status.invested < Decimal(301)  # three ~$100 buys (fractional rounding)
    assert status.value is not None
    # strategy == benchmark ticker here, so sim should track SPY benchmark closely
    assert status.benchmark_value is not None
    assert abs(status.value - status.benchmark_value) < Decimal("0.01") * status.benchmark_value


def test_momentum_picks_strongest(sim: SimService, provider: FakeProvider) -> None:
    provider.add_symbol("HOT", price="150.00", type_=InstrumentType.STOCK)
    provider.add_symbol("COLD", price="50.00", type_=InstrumentType.STOCK)
    watch = WatchlistService(sim.conn, provider)
    watch.add("HOT")
    watch.add("COLD")
    _seed_bars(sim.conn, "HOT", days=100, start_price=100.0, daily_gain=0.5)  # rising
    _seed_bars(sim.conn, "COLD", days=100, start_price=100.0, daily_gain=-0.4)  # falling
    sim.init(Decimal(100), strategy="momentum", ticker=None)
    _, symbol = sim.invest()
    assert symbol == "HOT"


def test_momentum_needs_watchlist(sim: SimService) -> None:
    sim.init(Decimal(100), strategy="momentum", ticker=None)
    with pytest.raises(TrdError, match="watchlist"):
        sim.invest()


def test_status_without_init_raises(sim: SimService) -> None:
    with pytest.raises(TrdError, match="No simulation account"):
        sim.status()
