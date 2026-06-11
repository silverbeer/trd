from decimal import Decimal
from pathlib import Path

import pytest

from trd.errors import TrdError
from trd.services import PortfolioService

CSV = """date,account,symbol,side,quantity,price,fees,note
2026-01-05,main,AAPL,buy,10,150.00,1.00,initial position
2026-02-10,main,BTC-USD,buy,0.05,95000,0,
2026-03-01,main,AAPL,sell,4,180.00,1.00,trim
"""


def test_import_csv(portfolio: PortfolioService, tmp_path: Path) -> None:
    path = tmp_path / "txns.csv"
    path.write_text(CSV)
    assert portfolio.import_csv(path) == 3
    positions = {p.instrument.symbol: p for p in portfolio.positions()}
    assert positions["AAPL"].quantity == Decimal(6)
    assert positions["BTC-USD"].quantity == Decimal("0.05")


def test_import_missing_columns(portfolio: PortfolioService, tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("symbol,side\nAAPL,buy\n")
    with pytest.raises(TrdError, match="missing columns"):
        portfolio.import_csv(path)


def test_import_bad_row_reports_line(portfolio: PortfolioService, tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "date,account,symbol,side,quantity,price\n2026-01-05,main,AAPL,buy,notanumber,150\n"
    )
    with pytest.raises(TrdError, match=":2:"):
        portfolio.import_csv(path)
