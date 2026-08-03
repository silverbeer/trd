"""A manual trade must not desync the engine's book from the account's.

`engine_position` carries its own `quantity`, and nothing in the manual trade path
updates it. Before this guard, selling part of an engine-held symbol was accepted
silently: the engine kept believing it held the original size, later sold all of
it, and a long-only paper account ended up short — with no error raised at any
step.

Reproduced on a copy of the live engine database on 2026-08-03:

    manual sell of 1.8 AMZN     -> accepted, no warning
    engine_position.quantity     -> still 3.75996400
    actual FIFO holding          -> 1.95996400
    engine exits its position    -> sells 3.75996400
    net AMZN across all txns     -> -1.80000000

`test_the_desync_scenario_is_refused` is that scenario, now blocked.
"""

from datetime import date, datetime
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.errors import EnginePositionConflictError
from trd.models import AccountType, Side
from trd.services import EngineService, PortfolioService


@pytest.fixture
def engine_with_position(
    conn: duckdb.DuckDBPyConnection, provider: FakeProvider
) -> tuple[EngineService, PortfolioService, str]:
    """An engine holding exactly one open position in AAA."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    opened = engine.scan()
    assert len(opened.opened) == 1
    return engine, PortfolioService(conn, provider), engine.account().name


def test_the_desync_scenario_is_refused(engine_with_position) -> None:
    """The exact sequence that took the account short."""
    engine, portfolio, account = engine_with_position
    held = engine.position_rows(open_only=True)[0].position.quantity

    with pytest.raises(EnginePositionConflictError, match="held by the trading engine"):
        portfolio.record_trade(account, "AAA", Side.SELL, held / 2, price=Decimal("100"))


def test_a_manual_buy_is_refused_too(engine_with_position) -> None:
    """A buy does not go short, but it still leaves the two books disagreeing —
    the engine's exit would sell only its own quantity and orphan the rest."""
    _engine, portfolio, account = engine_with_position

    with pytest.raises(EnginePositionConflictError):
        portfolio.record_trade(account, "AAA", Side.BUY, Decimal("1"), price=Decimal("100"))


def test_the_error_names_the_symbol_and_account(engine_with_position) -> None:
    """An error that does not say what to do is a dead end."""
    _engine, portfolio, account = engine_with_position

    with pytest.raises(EnginePositionConflictError) as caught:
        portfolio.record_trade(account, "AAA", Side.SELL, Decimal("1"), price=Decimal("100"))

    message = str(caught.value)
    assert "AAA" in message
    assert account in message
    assert "trd engine positions" in message


def test_an_untouched_symbol_on_the_same_account_still_trades(
    engine_with_position, conn, provider
) -> None:
    """The guard is per symbol, not per account. The engine holding AAA says
    nothing about BBB."""
    _engine, portfolio, account = engine_with_position
    provider.add_symbol("BBB", price="50")

    txn = portfolio.record_trade(account, "BBB", Side.BUY, Decimal("2"), price=Decimal("50"))
    assert txn.quantity == Decimal("2")


def test_a_different_account_still_trades(engine_with_position, conn, provider) -> None:
    """Same symbol, different account — the engine's book is not involved."""
    _engine, portfolio, _account = engine_with_position
    portfolio.accounts.create("personal", AccountType.REAL)

    txn = portfolio.record_trade("personal", "AAA", Side.BUY, Decimal("1"), price=Decimal("100"))
    assert txn.quantity == Decimal("1")


def test_trading_resumes_once_the_engine_closes_the_position(engine_with_position) -> None:
    """The guard tracks open positions, so it lifts by itself. It must not
    permanently blacklist a symbol the engine once traded."""
    engine, portfolio, account = engine_with_position
    position = engine.position_rows(open_only=True)[0].position
    engine.positions.close(position.id, datetime.now(), Decimal("100"), "closed for the test")

    txn = portfolio.record_trade(account, "AAA", Side.BUY, Decimal("1"), price=Decimal("100"))
    assert txn.quantity == Decimal("1")


def test_the_engine_itself_is_unaffected(engine_with_position, conn) -> None:
    """The engine writes through TransactionRepo directly, not record_trade, so
    its own exits keep working. A guard that blocked the engine would be worse
    than the bug."""
    engine, _portfolio, _account = engine_with_position
    position = engine.position_rows(open_only=True)[0].position

    # Drop the quote through the stop and let the engine act.
    engine.provider.add_symbol(
        "AAA", price=str(float(position.stop_price) * 0.99), volume=1_200_000
    )
    result = engine.scan()

    assert len(result.closed) == 1
    assert result.closed[0].rule == "stop"
    assert engine.position_rows(open_only=True) == []


def test_bulk_import_is_guarded_too(engine_with_position, tmp_path) -> None:
    """`trd import` goes through the same method, so a CSV cannot smuggle a trade
    past the guard."""
    _engine, portfolio, account = engine_with_position
    csv = tmp_path / "txns.csv"
    csv.write_text(
        "date,account,symbol,side,quantity,price\n"
        f"{date.today().isoformat()},{account},AAA,sell,1,100\n"
    )

    with pytest.raises(EnginePositionConflictError):
        portfolio.import_csv(csv)
