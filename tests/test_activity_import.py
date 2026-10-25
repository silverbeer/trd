"""Importing a managed account's CSV exports (SB-1418).

Every fixture under tests/fixtures/activity is SYNTHETIC: The custodian's layout
(BOM, blank lines, header, newest-first rows, legal footer) and its action
phrasings, with invented quantities, prices, amounts and dates.

The fixtures are .txt, not .csv, on purpose: .gitignore keeps every *.csv out
of this public repo so a real export can never be committed by accident, and
that guard stays whole.

What is worth pinning: every action class lands where the module docstring
says, an advisory fee is a fee and lowers the return, nothing unknown is
guessed, and re-importing writes nothing twice.
"""

import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from trd.cli.app import app
from trd.errors import TrdError, UnknownAccountError
from trd.models import AccountType, DailyBar, IncomeKind, Side
from trd.models.activity_import import ActivityKind, WriteTarget
from trd.models.alloc import AllocTarget, CashFlowKind
from trd.repos import AccountRepo
from trd.repos.income import IncomeRepo
from trd.services.activity_import import (
    ActivityImportService,
    balance_check,
    parse_activity,
    parse_positions,
    plan_rows,
)
from trd.services.alloc import AllocService, CashService
from trd.services.cashflow import cash_flows, external_flows
from trd.services.dashboard import Dashboard
from trd.services.portfolio import PortfolioService
from trd.services.reconcile import ReconcileService
from trd.services.xirr import xirr

D = Decimal
FIX = Path(__file__).parent / "fixtures" / "activity"
ACCOUNT = "managed-ira"
runner = CliRunner()


def text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8-sig")


@pytest.fixture
def importer(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> ActivityImportService:
    return ActivityImportService(conn, provider)


def run(importer: ActivityImportService, name: str = "activity.txt", **kw):
    kw.setdefault("create", "Managed IRA")
    return importer.run(FIX / name, ACCOUNT, **kw)


# ------------------------------------------------------------------ parsing


def test_parse_skips_bom_blank_lines_and_footer() -> None:
    rows = parse_activity(text("activity.txt"))
    assert len(rows) == 15
    assert rows[0].run_date == date(2025, 8, 1)  # newest first, as written
    assert rows[-1].kind == ActivityKind.TRANSFER
    assert all(r.line > 3 for r in rows)  # header sits below the BOM and two blanks


def test_every_action_class_is_recognised() -> None:
    kinds = {r.kind for r in parse_activity(text("activity.txt"))}
    assert kinds == set(ActivityKind)


def test_quoted_and_empty_values_and_negative_sell_quantity() -> None:
    rows = parse_activity(text("activity.txt"))
    sell = next(r for r in rows if r.kind == ActivityKind.SELL)
    assert sell.quantity == D(-5)  # quoted "-5"
    assert sell.fees == D("0.05")  # Fees ($) set, Commission ($) empty ""
    assert sell.amount == D("1299.95")
    fee = next(r for r in rows if r.kind == ActivityKind.ADVISORY_FEE)
    assert fee.symbol is None and fee.price is None and fee.settlement_date is None


def test_running_cash_balance_replays_without_a_break() -> None:
    """The custodian's own Cash Balance column agrees with how every row is read —
    including the core money market reinvestment, which moves no cash."""
    breaks, opening = balance_check(parse_activity(text("activity.txt")))
    assert breaks == 0
    assert opening == 0


def test_unknown_action_refuses_the_whole_file_and_names_the_line() -> None:
    with pytest.raises(TrdError, match=r"line \d+: unknown action 'MYSTERY CORPORATE ACTION"):
        parse_activity(text("activity_unknown.txt"))


def test_not_an_activity_export_is_refused() -> None:
    with pytest.raises(TrdError, match="Run Date"):
        parse_activity("date,account,symbol\n2025-01-01,main,AAPL\n")


def test_contradictory_signs_are_refused() -> None:
    bad = text("activity.txt").replace('"-13",1979.5', "13,1979.5")
    with pytest.raises(TrdError, match="advisory fee needs a negative amount"):
        parse_activity(bad)


# ---------------------------------------------------------------- the plan


def test_rows_land_where_the_rules_say() -> None:
    writes = plan_rows(parse_activity(text("activity.txt")))
    by_kind: dict[str, list] = {}
    for w in writes:
        by_kind.setdefault(w.kind, []).append(w)
    assert len(by_kind["buy"]) == 6  # 4 bought + 2 reinvested
    assert len(by_kind["sell"]) == 1 and by_kind["sell"][0].quantity == D(5)
    assert by_kind["sell"][0].price == D(260)  # from the cash: (1299.95 + 0.05) / 5
    assert [w.amount for w in by_kind["dividend"]] == [D(9)]
    assert sorted(w.amount for w in by_kind["capital_gain"]) == [D(10), D(40)]
    # Core money market: its dividend is interest on cash; its reinvestment is nothing.
    (sweep,) = by_kind["cash_sweep"]
    assert sweep.symbol is None and sweep.amount == D(5)
    (core,) = by_kind["core_cash"]
    assert core.target == WriteTarget.NONE
    assert [w.amount for w in by_kind["fee"]] == [D("12.5"), D(13)]
    assert all(w.target == WriteTarget.CASH_FLOW for w in by_kind["fee"])
    assert [w.amount for w in by_kind["deposit"]] == [D(10000)]
    assert writes == sorted(writes, key=lambda w: w.on)  # oldest first, for FIFO


def test_identical_rows_on_one_day_get_distinct_keys() -> None:
    writes = plan_rows(parse_activity(text("activity.txt")))
    dupes = [w for w in writes if w.on == date(2025, 8, 1) and w.kind == "buy"]
    assert len(dupes) == 2
    assert dupes[0].key.endswith("|0") and dupes[1].key.endswith("|1")
    assert dupes[0].key[:-2] == dupes[1].key[:-2]


# ------------------------------------------------------------ the import


def test_import_writes_every_class(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection
) -> None:
    plan = run(importer)
    assert plan.applied and plan.create_account
    account = AccountRepo(conn).get_by_name(ACCOUNT)
    assert account is not None and account.type == AccountType.REAL

    held = ReconcileService(conn).held(ACCOUNT)
    assert held == {"FXAIX": D("20.2"), "FXNAX": D("400.9")}  # never SPAXX

    income = IncomeRepo(conn).list_all(account.id)
    assert sorted((i.kind.value, i.amount) for i in income) == [
        ("capital_gain", D(10)),
        ("capital_gain", D(40)),
        ("cash_sweep", D(5)),
        ("dividend", D(9)),
    ]
    balance = CashService(conn).balance(ACCOUNT)
    assert balance.fees == D("25.50")
    assert balance.deposits == D(10000) and balance.withdrawals == 0
    # Lands on the custodian's own closing Cash Balance for the file, to the cent.
    assert balance.cash == D("2279.45")


def test_reinvested_dividend_is_income_in_and_a_buy_out_once_each(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection
) -> None:
    run(importer)
    account = AccountRepo(conn).get_by_name(ACCOUNT)
    assert account is not None
    txns = PortfolioService(conn, FakeProvider()).txns.list_chronological(account.id)
    drip = [t for t in txns if t.executed_at.date() == date(2025, 3, 31)]
    assert [(t.side, t.quantity, t.quantity * t.price) for t in drip] == [
        (Side.BUY, D("0.9"), D(9))
    ]
    paid = [i for i in IncomeRepo(conn).list_all(account.id) if i.kind == IncomeKind.DIVIDEND]
    assert [i.amount for i in paid] == [D(9)]


def test_unpriced_fund_is_tracked_under_the_brokers_name(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection
) -> None:
    """The fake provider knows neither fund — as with adviser-only share classes."""
    run(importer)
    instrument = PortfolioService(conn, FakeProvider()).instruments.get_by_symbol("FXNAX")
    assert instrument is not None and instrument.name == "EXAMPLE US BOND INDEX FUND"


def test_reimport_writes_nothing(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection
) -> None:
    run(importer)
    before = CashService(conn).balance(ACCOUNT)
    again = run(importer)
    assert again.new_writes == 0 and again.already_imported == 15
    assert CashService(conn).balance(ACCOUNT) == before
    # Fees are still listed on a run that adds nothing.
    assert [f.already_imported for f in again.fees] == [True, True]
    assert again.fees_total == D("25.5")


def test_overlapping_file_imports_only_the_new_rows(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection
) -> None:
    run(importer)
    later = run(importer, "activity_later.txt")
    assert later.new_writes == 2  # the September pair; the August day is already in
    assert later.already_imported == 3
    assert ReconcileService(conn).held(ACCOUNT)["FXNAX"] == D("401.1")
    # The two identical August buys stay two, not one and not four.
    assert CashService(conn).balance(ACCOUNT).cash == D("2279.45")


def test_dry_run_writes_nothing(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection
) -> None:
    plan = run(importer, dry_run=True)
    assert plan.dry_run and not plan.applied
    assert plan.counts["fee"] == 2 and plan.fees_total == D("25.5")
    assert (plan.first_date, plan.last_date) == (date(2025, 3, 3), date(2025, 8, 1))
    assert AccountRepo(conn).get_by_name(ACCOUNT) is None
    assert conn.execute("SELECT count(*) FROM import_row").fetchone() == (0,)


def test_missing_account_without_create_is_refused(importer: ActivityImportService) -> None:
    with pytest.raises(UnknownAccountError):
        importer.run(FIX / "activity.txt", ACCOUNT)


def test_simulation_account_is_refused(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection
) -> None:
    AccountRepo(conn).create(ACCOUNT, AccountType.SIMULATION)
    with pytest.raises(TrdError, match="real money"):
        run(importer)


def test_a_failed_write_rolls_back_everything(
    importer: ActivityImportService,
    conn: duckdb.DuckDBPyConnection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args, **kwargs):
        raise TrdError("disk full")

    monkeypatch.setattr(importer.flows, "add", boom)
    with pytest.raises(TrdError, match="disk full"):
        run(importer)
    assert conn.execute("SELECT count(*) FROM txn").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM import_row").fetchone() == (0,)


# ------------------------------------------------------- opening + positions


def test_sells_before_any_holding_are_refused_without_positions(
    importer: ActivityImportService,
) -> None:
    with pytest.raises(TrdError, match="--opening-from-positions"):
        run(importer, "activity_mid_history.txt")
    # A dry run still prints the plan — and its fees — with the refusal attached.
    plan = run(importer, "activity_mid_history.txt", dry_run=True)
    assert plan.refused is not None and "FXAIX: sells 2, holds 0" in plan.refused
    assert plan.fees_total == D(10)


def _opening_bars(provider: FakeProvider) -> None:
    def bar(on: date, close: str) -> DailyBar:
        c = D(close)
        return DailyBar(date=on, open=c, high=c, low=c, close=c)

    provider.add_bars("FXAIX", [bar(date(2025, 4, 30), "205"), bar(date(2025, 5, 1), "999")])
    provider.add_bars("FXNAX", [bar(date(2025, 4, 28), "10")])  # on-or-before, not after


def test_opening_from_positions_seeds_the_day_before(
    importer: ActivityImportService,
    conn: duckdb.DuckDBPyConnection,
    provider: FakeProvider,
) -> None:
    _opening_bars(provider)
    plan = run(
        importer,
        "activity_mid_history.txt",
        positions=FIX / "positions_mid_history.txt",
        opening_from_positions=True,
    )
    opening = {w.symbol or w.kind: w for w in plan.writes if w.line is None}
    assert opening["FXAIX"].quantity == D(10)  # 8 now + 2 sold in the file
    assert opening["FXAIX"].price == D(205)
    assert opening["FXNAX"].quantity == D(100)
    # holdings 10 x 205 + 100 x 10, plus the 500 cash before the first row
    assert opening["deposit"].amount == D(3550)
    assert all(w.on == date(2025, 4, 30) for w in opening.values())
    assert plan.positions is not None and plan.positions.ok
    assert CashService(conn).balance(ACCOUNT).cash == D(910)

    again = run(
        importer,
        "activity_mid_history.txt",
        positions=FIX / "positions_mid_history.txt",
        opening_from_positions=True,
    )
    assert again.new_writes == 0


def test_opening_needs_a_price_and_takes_one_by_hand(
    importer: ActivityImportService, provider: FakeProvider
) -> None:
    with pytest.raises(TrdError, match="--opening-price"):
        run(
            importer,
            "activity_mid_history.txt",
            positions=FIX / "positions_mid_history.txt",
            opening_from_positions=True,
            dry_run=True,
        )
    plan = run(
        importer,
        "activity_mid_history.txt",
        positions=FIX / "positions_mid_history.txt",
        opening_from_positions=True,
        opening_prices={"FXAIX": D(200), "FXNAX": D(10)},
        dry_run=True,
    )
    assert plan.positions is not None and plan.positions.ok and plan.positions.projected


def test_positions_reconcile_match(importer: ActivityImportService) -> None:
    plan = run(importer, positions=FIX / "positions.txt")
    assert plan.positions is not None
    assert plan.positions.ok and not plan.positions.projected
    assert plan.positions.cash_broker == D("2279.45")
    assert "not yet verified" in plan.positions.note


def test_positions_reconcile_mismatch(importer: ActivityImportService) -> None:
    plan = run(importer, positions=FIX / "positions_mismatch.txt")
    assert plan.positions is not None and not plan.positions.ok
    assert plan.positions.mismatched == ["FXAIX"]
    (row,) = [r for r in plan.positions.rows if r.symbol == "FXAIX"]
    assert row.diff == D(-1)


def test_positions_parser_reads_by_header_name() -> None:
    parsed = parse_positions(text("positions.txt"))
    assert parsed.quantities() == {"FXAIX": D("20.2"), "FXNAX": D("400.9")}
    assert parsed.cash == D("2279.45")  # SPAXX** is cash; '$5,454.00' parses
    assert parsed.downloaded_at == datetime(2025, 9, 3, 10, 5)


def test_positions_with_several_accounts_need_one_named() -> None:
    with pytest.raises(TrdError, match="--source-account"):
        parse_positions(text("positions_two_accounts.txt"))
    other = parse_positions(text("positions_two_accounts.txt"), "other ira")
    assert other.quantities() == {"FXAIX": D(3)} and other.cash == D(90)


# ------------------------------------------------- a fee lowers the return


def test_a_fee_lowers_simple_return_and_xirr_by_exactly_the_fee(
    conn: duckdb.DuckDBPyConnection, provider: FakeProvider
) -> None:
    """Two identical accounts; one pays a fee. Booked as a withdrawal it would
    be excused — the owner "took" the money — and both would score the same."""
    provider.add_symbol("IVV", price="660", prev_close="660")
    today = date(2026, 6, 1)
    funded = today - timedelta(days=400)
    alloc = AllocService(conn, provider)
    targets = [AllocTarget(symbol="IVV", weight=D(95)), AllocTarget(symbol="CASH", weight=D(5))]
    for name in ("plain", "charged"):
        AccountRepo(conn).create(name, AccountType.REAL)
        alloc.create(name, name, D(10000), targets)
        alloc.cash.record(
            name, CashFlowKind.DEPOSIT, D(10000), datetime.combine(funded, datetime.min.time())
        )
        PortfolioService(conn, provider).record_trade(
            name,
            "IVV",
            Side.BUY,
            D(10),
            price=D(600),
            executed_at=datetime.combine(funded, datetime.min.time()),
        )
    alloc.cash.record("charged", CashFlowKind.FEE, D(100), datetime(2026, 3, 1))

    plain, charged = alloc.view("plain", today), alloc.view("charged", today)
    assert plain.total_value == D(10600) and charged.total_value == D(10500)
    assert charged.cash.net_deposits == plain.cash.net_deposits  # not a withdrawal
    assert plain.simple_return_pct is not None and charged.simple_return_pct is not None
    assert plain.simple_return_pct - charged.simple_return_pct == D(1)  # 100 / 10000
    assert charged.xirr == pytest.approx(xirr([(funded, -10000.0), (today, 10500.0)]))
    assert plain.xirr is not None and charged.xirr is not None and charged.xirr < plain.xirr


def test_fees_reach_the_trade_based_returns_as_money_out() -> None:
    from trd.models.alloc import CashFlow

    fee = CashFlow(
        id=1, account_id=1, kind=CashFlowKind.FEE, amount=D(25), occurred_at=datetime(2026, 1, 2)
    )
    deposit = fee.model_copy(update={"kind": CashFlowKind.DEPOSIT, "amount": D(1000)})
    assert cash_flows([], fees=[fee, deposit]) == [(date(2026, 1, 2), -25.0)]
    assert external_flows([fee, deposit]) == [(date(2026, 1, 2), D(1000))]

    base = Dashboard(
        value=D(1100),
        invested=D(1000),
        gains=D(100),
        today_change=D(0),
        holdings=[],
        winners=[],
        losers=[],
        positions_up=0,
        positions_down=0,
        xirr=None,
        benchmark_value=None,
        spy_today_pct=None,
    )
    assert base.model_copy(update={"fees": D(30)}).total_return_pct == D(7)


# ---------------------------------------------------------------------- CLI


def test_cli_dry_run_json_lists_fees_and_writes_nothing(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "import", "activity", str(FIX / "activity.txt"),
            "--account", ACCOUNT, "--create", "Managed IRA", "--dry-run", "--json",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True and payload["applied"] is False
    assert [f["amount"] for f in payload["fees"]] == ["12.5", "13"]
    assert payload["fees_total"] == "25.5"
    ls = runner.invoke(app, ["cash", "ls", "--account", ACCOUNT, "--json"])
    assert ls.exit_code == 1  # the account was never created


def test_cli_import_then_cash_ls_shows_the_advisory_fee(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        ["import", "activity", str(FIX / "activity.txt"), "-a", ACCOUNT, "--create", "Managed IRA"],
    )
    assert result.exit_code == 0, result.output
    assert "ADVISORY FEES" in result.output and "25.50" in result.output
    ls = runner.invoke(app, ["cash", "ls", "--account", ACCOUNT])
    assert "advisory fee" in ls.output
    js = json.loads(runner.invoke(app, ["cash", "ls", "--account", ACCOUNT, "--json"]).output)
    assert {f["kind"] for f in js["flows"]} == {"deposit", "fee"}
    assert js["balance"]["fees"] == "25.50"


def test_cli_missing_account_option_is_refused(cli_env: FakeProvider) -> None:
    result = runner.invoke(app, ["import", "activity", str(FIX / "activity.txt")])
    assert result.exit_code != 0
    # CI renders Typer's error box in colour and wraps it, so match on the text
    # with ANSI codes and box-drawing stripped rather than the raw output.
    plain = re.sub(r"\x1b\[[0-9;]*m|[│╭╮╰╯─]|\s+", "", result.output)
    assert "--account" in plain


def test_cli_positions_mismatch_exits_1(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "import", "activity", str(FIX / "activity.txt"), "-a", ACCOUNT,
            "--create", "Managed IRA", "--positions", str(FIX / "positions_mismatch.txt"),
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "DO NOT match" in result.output


def test_cli_unknown_action_is_an_error_in_json(cli_env: FakeProvider) -> None:
    runner.invoke(app, ["init"])
    result = runner.invoke(
        app,
        [
            "import", "activity", str(FIX / "activity_unknown.txt"), "-a", ACCOUNT,
            "--create", "Managed IRA", "--json",
        ],
    )  # fmt: skip
    assert result.exit_code == 1
    assert "unknown action" in json.loads(result.output)["message"]


def test_backup_carries_the_import_ledger(
    importer: ActivityImportService, conn: duckdb.DuckDBPyConnection, tmp_path: Path
) -> None:
    """A restore that dropped the ledger would let the next import of the same
    export write every row twice."""
    from trd.db.connection import connect
    from trd.services.backup import export_data, restore_data

    run(importer)
    data = json.loads(json.dumps(export_data(conn), default=str))
    assert data["version"] == 11 and len(data["import_rows"]) == 15
    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    again = ActivityImportService(fresh, FakeProvider()).run(FIX / "activity.txt", ACCOUNT)
    assert again.new_writes == 0
    assert CashService(fresh).balance(ACCOUNT).cash == D("2279.45")
