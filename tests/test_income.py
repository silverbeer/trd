"""Dividends: the cash that never reaches FIFO and always reaches the return.

Two properties carry this feature, and both are easy to break quietly.

First, income must not touch holdings. A dividend creates no shares and consumes
none; if it ever reached the lot arithmetic it would corrupt cost basis and
every realised gain computed from it. That is why it lives in its own table
rather than as a third value on `Side`, and the test below is what keeps the
separation honest.

Second, it must reach every return that measures money. Three places build XIRR
flows — the dashboard, the equity curve and a plan — and before this they each
had their own copy of "a buy is negative, a sell is positive". Income added to
two of three is worse than income added to none, because the numbers then
disagree between screens.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from trd.cli.app import app
from trd.models import Income, IncomeKind, Side, Transaction
from trd.repos.income import IncomeRepo
from trd.services.cashflow import cash_flows, income_flows, plan_income_flows, trade_flows

runner = CliRunner()
DAY = date(2026, 7, 1)


def txn(symbol_id: int, side: str, qty: str, price: str, on: date, fees: str = "0") -> Transaction:
    return Transaction(
        id=0,
        account_id=1,
        instrument_id=symbol_id,
        side=Side(side),
        quantity=Decimal(qty),
        price=Decimal(price),
        fees=Decimal(fees),
        executed_at=datetime.combine(on, datetime.min.time()),
    )


def income(amount: str, on: date, instrument_id: int | None = 1) -> Income:
    return Income(
        id=0,
        account_id=1,
        instrument_id=instrument_id,
        kind=IncomeKind.DIVIDEND,
        amount=Decimal(amount),
        received_at=datetime.combine(on, datetime.min.time()),
    )


# ------------------------------------------------------------------ the signs


def test_money_out_is_negative_and_money_in_is_positive() -> None:
    """The whole of the arithmetic, and the reason a dividend raises a return:
    cash arriving has the same sign as a sale, without the share count moving."""
    flows = cash_flows([txn(1, "buy", "10", "100", DAY)], [income("5", DAY)])
    assert sorted(f[1] for f in flows) == [-1000.0, 5.0]


def test_fees_are_a_cost_on_both_sides() -> None:
    buy = trade_flows([txn(1, "buy", "1", "100", DAY, fees="2")])
    sell = trade_flows([txn(1, "sell", "1", "100", DAY, fees="2")])
    assert buy[0][1] == -102.0
    assert sell[0][1] == 98.0


def test_flows_come_back_in_date_order() -> None:
    """A caller appending its terminal value should not have to sort first."""
    flows = cash_flows(
        [txn(1, "buy", "1", "100", DAY + timedelta(days=10))],
        [income("5", DAY)],
    )
    assert [f[0] for f in flows] == [DAY, DAY + timedelta(days=10)]


def test_the_window_excludes_its_opening_date_and_includes_its_end() -> None:
    """The equity curve carries opening holdings as one outflow on the window's
    first day. A trade on that same day counted again would be double-charged."""
    end = DAY + timedelta(days=5)
    flows = cash_flows(
        [txn(1, "buy", "1", "100", DAY), txn(1, "buy", "1", "100", end)],
        [income("5", DAY), income("7", end)],
        after=DAY,
        until=end,
    )
    assert sorted(f[1] for f in flows) == [-100.0, 7.0]


def test_income_after_the_window_is_not_counted() -> None:
    assert income_flows([income("5", DAY + timedelta(days=30))], until=DAY) == []


# ------------------------------------------------- a plan's share of a payment


def test_a_plan_gets_only_its_share_of_a_dividend() -> None:
    """The sofi case. An account holding SPY from an old one-off and from one
    plan contribution must not credit the plan with the whole payment — that
    would flatter the very comparison the plan exists to make."""
    account = [txn(1, "buy", "3", "100", DAY - timedelta(days=30)), txn(1, "buy", "1", "100", DAY)]
    plan = [account[1]]
    flows = plan_income_flows([income("8", DAY)], plan, account)
    assert flows == [(DAY, 2.0)]  # the plan holds 1 of 4 shares


def test_a_plan_gets_none_of_a_dividend_on_something_it_never_bought() -> None:
    """VOO pays the sofi account; the plan has never held VOO."""
    account = [txn(2, "buy", "5", "500", DAY - timedelta(days=30))]
    plan = [txn(1, "buy", "1", "100", DAY)]
    assert plan_income_flows([income("8", DAY, instrument_id=2)], plan, account) == []


def test_account_level_income_is_never_attributed_to_a_plan() -> None:
    """Interest and sweep payments are paid on idle cash, not on a holding."""
    plan = [txn(1, "buy", "1", "100", DAY)]
    assert plan_income_flows([income("1", DAY, instrument_id=None)], plan, plan) == []


def test_the_share_is_taken_on_the_day_the_dividend_was_paid() -> None:
    """A contribution made after the payment cannot earn a share of it."""
    later = DAY + timedelta(days=20)
    account = [txn(1, "buy", "1", "100", DAY - timedelta(days=5)), txn(1, "buy", "3", "100", later)]
    flows = plan_income_flows([income("8", DAY)], [account[1]], account)
    assert flows == []  # the plan held nothing on DAY


# --------------------------------------------------------------- against a DB


@pytest.fixture
def home(cli_env: FakeProvider) -> FakeProvider:
    """An initialised throwaway database on the shared CLI fixture.

    `cli_env` already owns TRD_HOME and the fake provider; a second fixture
    setting its own TRD_HOME would init one database and let the commands write
    to another.
    """
    assert runner.invoke(app, ["init"]).exit_code == 0
    return cli_env


def test_a_dividend_changes_the_return_and_not_the_holdings(home: FakeProvider) -> None:
    """The property that justifies a separate table. If income ever reached FIFO,
    cost basis and every realised gain drawn from it would be wrong."""
    home.add_symbol("AAA", price="100", volume=1_000)
    assert runner.invoke(app, ["buy", "AAA", "10", "--price", "100"]).exit_code == 0

    before = runner.invoke(app, ["portfolio", "--json"])
    add = runner.invoke(app, ["income", "add", "25", "--symbol", "AAA"])
    assert add.exit_code == 0, add.output
    after = runner.invoke(app, ["portfolio", "--json"])
    assert before.output == after.output  # not one share, not one cent of basis


def test_a_dividend_needs_the_holding_that_paid_it(home: FakeProvider) -> None:
    """Unattached, it could never be split across a plan's share of a holding,
    and would be unexplainable a year later."""
    result = runner.invoke(app, ["income", "add", "25"])
    assert result.exit_code == 1
    assert "paid by a holding" in result.output


def test_account_level_cash_needs_no_symbol(home: FakeProvider) -> None:
    result = runner.invoke(app, ["income", "add", "0.01", "--kind", "cash_sweep"])
    assert result.exit_code == 0, result.output


def test_negative_income_is_refused(home: FakeProvider) -> None:
    """It would flow through XIRR as a contribution and improve the very return
    it was meant to correct."""
    # After "--" so click reads it as an argument, not a short option.
    result = runner.invoke(app, ["income", "add", "--kind", "interest", "--", "-5"])
    assert result.exit_code == 1
    assert "must be positive" in result.output


def test_an_unknown_kind_lists_the_ones_that_exist(home: FakeProvider) -> None:
    result = runner.invoke(app, ["income", "add", "5", "--kind", "coupon"])
    assert result.exit_code == 1
    assert "dividend" in result.output


def test_income_survives_a_backup_and_restore(home: FakeProvider, tmp_path) -> None:
    """No provider can hand this back — it is which payments landed in YOUR
    account, not what a symbol has ever paid. Same argument as the transactions."""
    home.add_symbol("AAA", price="100", volume=1_000)
    runner.invoke(app, ["buy", "AAA", "10", "--price", "100"])
    runner.invoke(app, ["income", "add", "1.98", "--symbol", "AAA", "--date", "2026-07-01"])
    runner.invoke(app, ["income", "add", "0.01", "--kind", "cash_sweep"])

    path = tmp_path / "backup.json"
    assert runner.invoke(app, ["backup", str(path)]).exit_code == 0
    assert runner.invoke(app, ["restore", str(path), "--force"]).exit_code == 0

    listed = runner.invoke(app, ["income", "ls", "--json"])
    assert listed.exit_code == 0, listed.output
    import json

    rows = json.loads(listed.output)
    assert {r["symbol"] for r in rows} == {"AAA", None}
    assert sum(Decimal(r["amount"]) for r in rows) == Decimal("1.99")


def test_the_backup_version_refuses_to_be_read_by_older_code() -> None:
    """A v4 reader handed a v5 file would ignore the income section and restore
    silently, losing every dividend rather than refusing."""
    from trd.services.backup import BACKUP_VERSION, SUPPORTED_VERSIONS

    assert BACKUP_VERSION == 5
    assert 4 in SUPPORTED_VERSIONS  # older files still read


def test_repo_totals_are_scoped_to_one_account(home: FakeProvider) -> None:
    from trd.config import get_settings
    from trd.db.connection import connect

    conn: duckdb.DuckDBPyConnection = connect(get_settings().db_path)
    try:
        repo = IncomeRepo(conn)
        repo.add(1, Decimal("5"), datetime(2026, 7, 1), IncomeKind.INTEREST)
        assert repo.total(1) == Decimal("5")
        assert repo.total(999) == Decimal(0)
    finally:
        conn.close()
