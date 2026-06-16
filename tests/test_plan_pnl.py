from decimal import Decimal

import duckdb
from rich.console import Console

from trd.cli.render import plans_pnl_table
from trd.services import PlanService

from .conftest import FakeProvider


def test_plans_pnl_table_renders(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> None:
    provider.add_symbol("SPY", price="500.00", prev_close="499.00")
    plans = PlanService(conn, provider)
    plans.set_plan("sim", Decimal(100), create_simulation=True, ticker="AAPL")
    plans.invest("sim")

    statuses = [plans.status(p.account.name) for p in plans.list_plans()]
    assert len(statuses) == 1
    assert statuses[0].invested == Decimal("100")

    console = Console(width=140)
    with console.capture() as cap:
        console.print(plans_pnl_table(statuses))
    text = cap.get()
    assert "DCA plans" in text
    assert "sim" in text
    assert "Invested" in text
