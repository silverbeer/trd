from datetime import datetime
from decimal import Decimal

import pytest

from trd.errors import InsufficientPositionError, UnknownAccountError, UnknownSymbolError
from trd.models import Side
from trd.services import PortfolioService


def test_buy_creates_instrument_from_provider(portfolio: PortfolioService) -> None:
    portfolio.record_trade("main", "aapl", Side.BUY, Decimal(10), Decimal(150))
    instrument = portfolio.instruments.get_by_symbol("AAPL")
    assert instrument is not None
    assert instrument.name == "AAPL Inc"
    assert instrument.type == "stock"


def test_buy_without_price_uses_live_quote(portfolio: PortfolioService) -> None:
    txn = portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(2))
    assert txn.price == Decimal("200.00")


def test_unknown_account_raises(portfolio: PortfolioService) -> None:
    with pytest.raises(UnknownAccountError):
        portfolio.record_trade("nope", "AAPL", Side.BUY, Decimal(1), Decimal(1))


def test_oversell_rejected(portfolio: PortfolioService) -> None:
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(5), Decimal(100), executed_at=datetime(2026, 1, 1)
    )
    with pytest.raises(InsufficientPositionError):
        portfolio.record_trade("main", "AAPL", Side.SELL, Decimal(6), Decimal(120))


def test_positions_math(portfolio: PortfolioService) -> None:
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(10), Decimal(150), executed_at=datetime(2026, 1, 1)
    )
    portfolio.record_trade(
        "main", "AAPL", Side.SELL, Decimal(4), Decimal(180), executed_at=datetime(2026, 2, 1)
    )
    [position] = portfolio.positions()
    assert position.quantity == Decimal(6)
    assert position.cost_basis == Decimal(900)  # 6 remaining @ 150
    assert position.price == Decimal("200.00")  # live fake quote
    assert position.market_value == Decimal("1200.00")
    assert position.unrealized_pl == Decimal("300.00")
    assert position.day_change == Decimal("30.00")  # (200-195) * 6
    assert not position.price_stale


def test_closed_positions_hidden(portfolio: PortfolioService) -> None:
    portfolio.record_trade(
        "main", "NVDA", Side.BUY, Decimal(3), Decimal(100), executed_at=datetime(2026, 1, 1)
    )
    portfolio.record_trade(
        "main", "NVDA", Side.SELL, Decimal(3), Decimal(110), executed_at=datetime(2026, 2, 1)
    )
    assert portfolio.positions() == []


def test_quote_failure_falls_back_to_snapshot(portfolio: PortfolioService, provider) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(150))
    portfolio.positions()  # stores a snapshot at 200
    provider.drop_quote("AAPL")
    [position] = portfolio.positions()
    assert position.price_stale
    assert position.price == Decimal("200.00")


def test_quote_failure_without_snapshot_shows_no_price(
    portfolio: PortfolioService, provider
) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(150))
    provider.drop_quote("AAPL")
    [position] = portfolio.positions()
    assert position.price is None
    assert position.market_value is None


def test_unknown_symbol_surfaces_provider_error(portfolio: PortfolioService) -> None:
    from trd.errors import ProviderError

    with pytest.raises((ProviderError, UnknownSymbolError)):
        portfolio.record_trade("main", "ZZZZZZ", Side.BUY, Decimal(1), Decimal(1))


def test_positions_scoped_to_account(portfolio: PortfolioService) -> None:
    from trd.models import AccountType

    portfolio.accounts.create("ira", AccountType.REAL)
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    portfolio.record_trade("ira", "NVDA", Side.BUY, Decimal(2), Decimal(100))
    assert [p.instrument.symbol for p in portfolio.positions("ira")] == ["NVDA"]
    assert len(portfolio.positions()) == 2
