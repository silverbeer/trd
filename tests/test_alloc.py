"""The Rollover IRA's allocation plan (SB-1383).

What is worth pinning: that a plan cannot claim money was invested that was not
(milestones are read from recorded buys), that a schedule which does not add up
to the capital is refused rather than silently short, that drift is measured on
the money in funds and not against cash waiting by design, that a missing price
is never a zero, and that the two proposal modes do what their docstrings say.
"""

import json
from datetime import date, datetime
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from trd.cli.app import app
from trd.errors import TrdError
from trd.models import AccountType, Side
from trd.models.alloc import CASH, AllocTarget, CashFlowKind, Milestone, MilestoneStatus
from trd.repos import AccountRepo
from trd.services.alloc import (
    AllocService,
    CashService,
    add_months,
    milestone_dates,
    progress,
    validate_plan,
)
from trd.services.backup import export_data, restore_data
from trd.services.portfolio import PortfolioService

runner = CliRunner()
D = Decimal
IRA = "***REMOVED***"
START = date(2026, 10, 26)
TARGETS = [
    AllocTarget(symbol="IVV", weight=D(45)),
    AllocTarget(symbol="IXUS", weight=D(20)),
    AllocTarget(symbol="QQQM", weight=D(25)),
    AllocTarget(symbol="SMH", weight=D(5)),
    AllocTarget(symbol=CASH, weight=D(5)),
]


@pytest.fixture
def ira(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> AllocService:
    for symbol, price in [("IVV", "600"), ("IXUS", "80"), ("QQQM", "250"), ("SMH", "300")]:
        provider.add_symbol(symbol, price=price, prev_close=price)
    AccountRepo(conn).create(IRA, AccountType.REAL)
    service = AllocService(conn, provider)
    service.create(
        IRA,
        "Fidelity Rollover IRA Growth Plan",
        D("***REMOVED***"),
        TARGETS,
        account_kind="Rollover IRA",
        brokerage="Fidelity",
    )
    return service


def buy(conn, provider, symbol: str, qty: str, price: str, on: date = START) -> None:
    PortfolioService(conn, provider).record_trade(
        account_name=IRA,
        symbol=symbol,
        side=Side.BUY,
        quantity=D(qty),
        price=D(price),
        executed_at=datetime.combine(on, datetime.min.time()).replace(hour=11),
    )


# ------------------------------------------------------------------ validation


def test_targets_must_sum_to_100_and_be_unique_and_positive() -> None:
    validate_plan(D(1000), TARGETS, D(5))
    with pytest.raises(TrdError, match="sum to 100, got 95"):
        validate_plan(D(1000), TARGETS[:-1], D(5))
    with pytest.raises(TrdError, match="twice"):
        validate_plan(D(1000), [*TARGETS[:-1], AllocTarget(symbol="IVV", weight=D(5))], D(5))
    with pytest.raises(TrdError, match="positive"):
        validate_plan(
            D(1000),
            [AllocTarget(symbol="IVV", weight=D(0)), AllocTarget(symbol="SMH", weight=D(100))],
            D(5),
        )


def test_a_schedule_that_does_not_reconcile_is_refused(ira: AllocService) -> None:
    """The prompt's own defaults: 150,000 + 4 x 25,000 = 250,000, not ***REMOVED***."""
    with pytest.raises(TrdError, match=r"930\.00 unaccounted"):
        ira.schedule(IRA, START, D(150000), D(25000), 3, 4)
    plan = ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    assert [m.due_date for m in plan.milestones] == [
        date(2026, 10, 26),
        date(2027, 1, 26),
        date(2027, 4, 26),
        date(2027, 7, 26),
        date(2027, 10, 26),
    ]
    assert plan.scheduled == D("***REMOVED***")
    with pytest.raises(TrdError, match="--replace"):
        ira.schedule(IRA, START, D(150930), D(25000), 3, 4)


def test_months_clamp_to_the_end_of_a_short_month() -> None:
    assert add_months(date(2027, 1, 31), 1) == date(2027, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    rows = milestone_dates(date(2026, 8, 31), D(100), D(10), 3, 2)
    assert [r[1] for r in rows] == [date(2026, 8, 31), date(2026, 11, 30), date(2027, 2, 28)]


# ------------------------------------------------------------------ progress


def ms(seq: int, due: date, amount: str, status=MilestoneStatus.PLANNED) -> Milestone:
    return Milestone(id=seq, seq=seq, due_date=due, amount=D(amount), status=status)


def test_milestones_are_filled_in_order_by_what_was_actually_bought() -> None:
    plan = [
        ms(0, date(2026, 10, 26), "150930"),
        ms(1, date(2027, 1, 26), "25000"),
        ms(2, date(2027, 4, 26), "25000"),
    ]
    states = lambda dep, today: [p.state for p in progress(plan, D(dep), today)]  # noqa: E731
    assert states("0", date(2026, 10, 1)) == ["planned", "planned", "planned"]
    assert states("0", date(2026, 11, 1)) == ["overdue", "planned", "planned"]
    assert states("100000", date(2026, 11, 1)) == ["partial", "planned", "planned"]
    # Whole-share fills land a few dollars short of a round tranche: still done.
    assert states("150912", date(2026, 11, 1)) == ["completed", "planned", "planned"]
    assert states("160930", date(2027, 2, 1)) == ["completed", "partial", "planned"]


def test_a_cancelled_milestone_takes_no_money_and_a_postponed_one_waits() -> None:
    plan = [
        ms(0, date(2026, 10, 26), "100"),
        ms(1, date(2027, 1, 26), "50", MilestoneStatus.CANCELLED),
        ms(2, date(2027, 6, 26), "50", MilestoneStatus.POSTPONED),
    ]
    rows = progress(plan, D("120"), date(2027, 3, 1))
    assert [r.state for r in rows] == ["completed", "cancelled", "partial"]
    assert rows[2].invested_toward == D(20)
    assert rows[2].cumulative_planned == D(150)


# ------------------------------------------------------------------ cash


def test_cash_is_deposits_less_buys_plus_sells_and_income(conn, provider, ira) -> None:
    cash = CashService(conn)
    cash.record(IRA, CashFlowKind.DEPOSIT, D("***REMOVED***"), datetime(2026, 10, 24))
    buy(conn, provider, "IVV", "100", "600")
    bal = cash.balance(IRA)
    assert bal.net_deposits == D("***REMOVED***")
    assert bal.cash == D("190930")
    with pytest.raises(TrdError, match="positive"):
        cash.record(IRA, CashFlowKind.DEPOSIT, D("-5"), datetime(2026, 10, 24))


# ------------------------------------------------------------------ view


def test_drift_is_measured_on_the_money_in_funds_not_the_cash_waiting(conn, provider, ira) -> None:
    """After the initial tranche most of the account is cash by design. Against
    the whole account every fund would read 35 points underweight."""
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("***REMOVED***"), datetime(2026, 10, 24))
    ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    # 45/20/25/5 of ~143,383 (150,930 minus the CASH share), at the fake prices.
    buy(conn, provider, "IVV", "113", "600")  # 67,800
    buy(conn, provider, "IXUS", "377", "80")  # 30,160
    buy(conn, provider, "QQQM", "151", "250")  # 37,750
    buy(conn, provider, "SMH", "25", "300")  # 7,500
    view = ira.view(IRA, today=date(2026, 10, 27))
    rows = {r.symbol: r for r in view.rows}
    assert view.deployed == D("143210")
    assert view.cash.cash == D("107720")
    assert view.total_value == D("***REMOVED***")
    assert view.gain == 0
    assert all(rows[s].action == "on target" for s in ("IVV", "IXUS", "QQQM", "SMH"))
    assert rows["IVV"].invested_target_pct is not None
    assert abs(rows["IVV"].invested_target_pct - D("47.368")) < D("0.001")  # 45/95
    # 95% of the initial 150,930 goes into funds (5% CASH stays cash): 143,383.50.
    # 143,210 bought is within tolerance of it: done, not "partial" for ever.
    assert view.progress[0].fund_target == D("143383.50")
    assert view.progress[0].state == "completed"
    assert view.remaining_to_deploy == D("***REMOVED***") * D("0.95") - D("143210")


def test_a_missing_price_is_never_a_zero(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 24))
    buy(conn, provider, "IVV", "10", "600")
    del provider.quotes["IVV"]
    view = ira.view(IRA)
    assert view.unpriced == ["IVV"]
    assert view.holdings_value is None and view.total_value is None and view.gain is None
    assert all(r.drift_pp is None for r in view.rows)


def test_a_holding_outside_the_plan_is_shown_not_hidden(conn, provider, ira) -> None:
    provider.add_symbol("TSLA", price="400")
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("1000"), datetime(2026, 10, 24))
    buy(conn, provider, "TSLA", "1", "400")
    view = ira.view(IRA)
    assert [r.symbol for r in view.other_holdings] == ["TSLA"]
    assert view.total_value == D("1000")


# ------------------------------------------------------------------ proposals


def test_target_mode_splits_by_weight_and_holds_back_the_cash_share(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("***REMOVED***"), datetime(2026, 10, 24))
    p = ira.proposal(IRA, amount=D("150930"), mode="target")
    legs = {leg.symbol: leg for leg in p.legs}
    assert p.held_as_cash == D("7546.50")
    assert sum(leg.amount for leg in p.legs) == D("143383.50")
    assert legs["IVV"].amount == D("67918.50")  # 143,383.50 x 45/95
    assert legs["IVV"].shares == D("113.197")  # rounded down: never more than the amount buys
    assert not any("more than" in n for n in p.notes)


def test_underweight_first_fills_the_gaps_and_skips_the_overweight(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("100000"), datetime(2026, 10, 24))
    buy(conn, provider, "IVV", "100", "600")  # 60,000: everything is in IVV
    p = ira.proposal(IRA, amount=D("20000"), mode="underweight")
    legs = {leg.symbol: leg.amount for leg in p.legs}
    assert legs["IVV"] == 0
    assert any("IVV" in n for n in p.notes)
    assert sum(legs.values()) == D("19000")  # 20,000 less the 5% CASH share
    # Shared in proportion to the gaps below (60,000 + 19,000) x renormalised weight.
    assert legs["IXUS"] > legs["SMH"] > 0


def test_underweight_first_spreads_the_remainder_once_every_gap_is_filled(
    conn, provider, ira
) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("1000000"), datetime(2026, 10, 24))
    buy(conn, provider, "IVV", "10", "600")  # a tiny sleeve
    p = ira.proposal(IRA, amount=D("95000"), mode="underweight")
    after = {leg.symbol: leg.weight_after for leg in p.legs}
    for leg in p.legs:
        assert leg.weight_after is not None and leg.target_pct is not None
        assert abs(leg.weight_after - leg.target_pct) < D("0.5"), after


def test_underweight_first_refuses_without_a_price_for_a_held_fund(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 24))
    buy(conn, provider, "IVV", "5", "600")
    del provider.quotes["IVV"]
    with pytest.raises(TrdError, match="missing: IVV"):
        ira.proposal(IRA, amount=D("5000"), mode="underweight")
    target = ira.proposal(IRA, amount=D("5000"), mode="target")  # needs no values
    assert {leg.symbol: leg.shares for leg in target.legs}["IVV"] is None


def test_a_proposal_says_when_there_is_not_the_cash_for_it(conn, provider, ira) -> None:
    p = ira.proposal(IRA, amount=D("150930"), mode="target")  # nothing deposited yet
    assert any("Record the deposit first" in n for n in p.notes)


def test_an_edited_leg_is_kept_and_the_rest_re_split(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("100000"), datetime(2026, 10, 24))
    p = ira.proposal(IRA, amount=D("10000"), mode="target", overrides={"SMH": D("1000")})
    legs = {leg.symbol: leg.amount for leg in p.legs}
    assert legs["SMH"] == D("1000")
    assert sum(legs.values()) == D("9500")
    with pytest.raises(TrdError, match="Not a fund"):
        ira.proposal(IRA, amount=D("10000"), overrides={"TSLA": D("1")})


def test_the_next_open_milestone_is_the_default_amount(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("***REMOVED***"), datetime(2026, 10, 24))
    ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    buy(conn, provider, "IVV", "100", "600")  # 60,000 of the initial 150,930
    p = ira.proposal(IRA, mode="target")
    # 143,383.50 for funds less 60,000 bought = 83,383.50, grossed up by the 5% CASH.
    assert p.amount == D("87772.11")
    assert sum(leg.amount for leg in p.legs) == D("83383.50")


# ------------------------------------------------------------------ history


def test_changes_are_kept_in_the_history(conn, provider, ira) -> None:
    ira.edit(IRA, targets=[*TARGETS[:3], AllocTarget(symbol="SMH", weight=D(10))])
    ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    ira.postpone(IRA, 1, date(2027, 2, 15), note="waiting for earnings season")
    text = " | ".join(e.what for e in ira.history(IRA))
    assert "CASH=5 → IVV=45 IQQ" not in text  # sanity: readable, not a repr
    assert "SMH=10" in text and "milestone 1: 2027-01-26 → 2027-02-15" in text
    assert "waiting for earnings season" in text


# ------------------------------------------------------------------ backup


def test_deposits_and_the_plan_survive_backup_and_restore(conn, provider, ira, tmp_path) -> None:
    CashService(conn).record(
        IRA, CashFlowKind.DEPOSIT, D("***REMOVED***"), datetime(2026, 10, 24), "rollover"
    )
    ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    ira.cancel(IRA, 4)
    data = json.loads(json.dumps(export_data(conn)))
    assert data["version"] == 8

    from trd.db.connection import connect

    fresh = connect(tmp_path / "restored.duckdb")
    stats = restore_data(fresh, data)
    assert (stats.cash_flows, stats.alloc_plans) == (1, 1)
    restored = AllocService(fresh, provider).get(IRA)
    assert restored.capital == D("***REMOVED***")
    assert [t.symbol for t in restored.targets] == ["IVV", "QQQM", "IXUS", CASH, "SMH"]
    assert restored.milestones[4].status == MilestoneStatus.CANCELLED
    assert CashService(fresh).balance(IRA).deposits == D("***REMOVED***")


# ------------------------------------------------------------------ CLI


def test_cli_end_to_end(cli_env: FakeProvider) -> None:
    for symbol, price in [("IVV", "600"), ("IXUS", "80"), ("QQQM", "250"), ("SMH", "300")]:
        cli_env.add_symbol(symbol, price=price, prev_close=price)
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["account", "add", IRA]).exit_code == 0
    result = runner.invoke(
        app,
        [
            "cash",
            "deposit",
            "***REMOVED***",
            "-a",
            IRA,
            "-d",
            "2026-10-24",
            "--note",
            "***REMOVED***",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "alloc",
            "set",
            "-a",
            IRA,
            "--capital",
            "***REMOVED***",
            "--name",
            "Growth Plan",
            "--kind",
            "Rollover IRA",
            "--brokerage",
            "Fidelity",
            "-t",
            "IVV=45",
            "-t",
            "IXUS=20",
            "-t",
            "QQQM=25",
            "-t",
            "SMH=5",
            "-t",
            "CASH=5",
        ],
    )
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        [
            "alloc",
            "schedule",
            "--start",
            "2026-10-26",
            "--initial",
            "150930",
            "--amount",
            "25000",
            "--count",
            "4",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2027-10-26" in result.output
    result = runner.invoke(app, ["alloc", "propose", "--mode", "target", "--json"])
    assert result.exit_code == 0, result.output
    proposal = json.loads(result.output)
    assert D(proposal["amount"]) == D("150930")
    result = runner.invoke(app, ["alloc", "show", "--json"])
    assert result.exit_code == 0, result.output
    view = json.loads(result.output)
    assert D(view["cash"]["cash"]) == D("***REMOVED***")
    assert D(view["total_value"]) == D("***REMOVED***")
    bad = runner.invoke(app, ["alloc", "edit", "-t", "IVV=50"])
    assert bad.exit_code == 1 and "sum to 100" in bad.output
