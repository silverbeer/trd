from datetime import datetime
from decimal import Decimal

from tests.test_fifo import txn
from trd.models import Side
from trd.services import PortfolioService
from trd.services.fifo import open_lots


def test_open_lots_keeps_buy_dates_and_prices() -> None:
    lots = open_lots(
        [
            txn(Side.BUY, "10", "100", fees="2", when=0),
            txn(Side.BUY, "5", "200", when=1),
        ]
    )
    assert len(lots) == 2
    assert lots[0].bought_at == datetime(2026, 1, 1)
    assert lots[0].price == Decimal(100)
    assert lots[0].cost == Decimal(1002)
    assert lots[1].bought_at == datetime(2026, 1, 2)


def test_open_lots_partial_sell_keeps_original_price() -> None:
    lots = open_lots(
        [
            txn(Side.BUY, "10", "100", when=0),
            txn(Side.BUY, "10", "200", when=1),
            txn(Side.SELL, "15", "300", when=2),
        ]
    )
    assert len(lots) == 1
    assert lots[0].price == Decimal(200)  # original per-share price survives
    assert lots[0].quantity == Decimal(5)
    assert lots[0].cost == Decimal(1000)


def test_service_lots_with_live_prices(portfolio: PortfolioService) -> None:
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(10), Decimal(150), executed_at=datetime(2025, 3, 1)
    )
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(5), Decimal(180), executed_at=datetime(2025, 9, 1)
    )
    portfolio.record_trade(
        "main", "NVDA", Side.BUY, Decimal(2), Decimal(90), executed_at=datetime(2025, 6, 1)
    )

    lots = portfolio.lots()
    assert [(lot.instrument.symbol, lot.bought_at.date().isoformat()) for lot in lots] == [
        ("AAPL", "2025-03-01"),
        ("AAPL", "2025-09-01"),
        ("NVDA", "2025-06-01"),
    ]
    first = lots[0]
    assert first.price_paid == Decimal(150)
    assert first.cost == Decimal(1500)
    assert first.price == Decimal("200.00")  # live fake quote
    assert first.value == Decimal("2000.00")
    assert first.gain == Decimal("500.00")


def test_service_lots_symbol_filter(portfolio: PortfolioService) -> None:
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    portfolio.record_trade("main", "NVDA", Side.BUY, Decimal(1), Decimal(100))
    lots = portfolio.lots(symbol="nvda")
    assert [lot.instrument.symbol for lot in lots] == ["NVDA"]


def test_lots_carry_account_name(portfolio: PortfolioService) -> None:
    from trd.models import AccountType

    portfolio.accounts.create("fidelity", AccountType.REAL)
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(1), Decimal(100), executed_at=datetime(2025, 1, 1)
    )
    portfolio.record_trade(
        "fidelity", "AAPL", Side.BUY, Decimal(2), Decimal(110), executed_at=datetime(2025, 2, 1)
    )
    lots = portfolio.lots()
    assert [(lot.account, lot.quantity) for lot in lots] == [
        ("main", Decimal(1)),
        ("fidelity", Decimal(2)),
    ]


def test_sell_never_consumes_other_accounts_lots(portfolio: PortfolioService) -> None:
    """FIFO is per account: selling at one broker must not touch lots at another,
    even when the other account's lot is older."""
    from trd.models import AccountType

    portfolio.accounts.create("fidelity", AccountType.REAL)
    # fidelity holds the OLDEST lot — merged FIFO would wrongly consume it
    portfolio.record_trade(
        "fidelity", "AAPL", Side.BUY, Decimal(5), Decimal(50), executed_at=datetime(2024, 1, 1)
    )
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(5), Decimal(150), executed_at=datetime(2025, 1, 1)
    )
    portfolio.record_trade(
        "main", "AAPL", Side.SELL, Decimal(5), Decimal(180), executed_at=datetime(2025, 6, 1)
    )

    lots = portfolio.lots()
    assert len(lots) == 1
    assert lots[0].account == "fidelity"
    assert lots[0].quantity == Decimal(5)
    assert lots[0].price_paid == Decimal(50)  # untouched original lot

    [position] = portfolio.positions()
    assert position.quantity == Decimal(5)
    assert position.cost_basis == Decimal(250)  # fidelity basis, not main's


def test_simulation_accounts_excluded_by_default(portfolio: PortfolioService) -> None:
    from trd.models import AccountType

    portfolio.accounts.create("paper", AccountType.SIMULATION)
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(1), Decimal(100), executed_at=datetime(2025, 1, 1)
    )
    portfolio.record_trade(
        "paper", "AAPL", Side.BUY, Decimal(9), Decimal(100), executed_at=datetime(2025, 1, 2)
    )

    # default: real money only
    [position] = portfolio.positions()
    assert position.quantity == Decimal(1)
    assert [lot.account for lot in portfolio.lots()] == ["main"]

    # --all includes paper
    [position_all] = portfolio.positions(include_simulation=True)
    assert position_all.quantity == Decimal(10)
    assert {lot.account for lot in portfolio.lots(include_simulation=True)} == {"main", "paper"}

    # naming the sim account explicitly always works
    [paper_position] = portfolio.positions("paper")
    assert paper_position.quantity == Decimal(9)


def test_service_lots_sold_out_position_absent(portfolio: PortfolioService) -> None:
    portfolio.record_trade(
        "main", "AAPL", Side.BUY, Decimal(5), Decimal(100), executed_at=datetime(2025, 1, 1)
    )
    portfolio.record_trade(
        "main", "AAPL", Side.SELL, Decimal(5), Decimal(120), executed_at=datetime(2025, 2, 1)
    )
    assert portfolio.lots() == []
