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
from trd.models import AccountType, DailyBar, InstrumentInfo, Side
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
from trd.services.alloc_report import AllocReportService
from trd.services.backup import export_data, restore_data
from trd.services.portfolio import PortfolioService

runner = CliRunner()
D = Decimal
IRA = "fidelity-ira"
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
        D("250930"),
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
    """The prompt's own defaults: 150,000 + 4 x 25,000 = 250,000, not 250,930."""
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
    assert plan.scheduled == D("250930")
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
    cash.record(IRA, CashFlowKind.DEPOSIT, D("250930"), datetime(2026, 10, 24))
    buy(conn, provider, "IVV", "100", "600")
    bal = cash.balance(IRA)
    assert bal.net_deposits == D("250930")
    assert bal.cash == D("190930")
    with pytest.raises(TrdError, match="positive"):
        cash.record(IRA, CashFlowKind.DEPOSIT, D("-5"), datetime(2026, 10, 24))


# ------------------------------------------------------------------ view


def test_drift_is_measured_on_the_money_in_funds_not_the_cash_waiting(conn, provider, ira) -> None:
    """After the initial tranche most of the account is cash by design. Against
    the whole account every fund would read 35 points underweight."""
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("250930"), datetime(2026, 10, 24))
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
    assert view.total_value == D("250930")
    assert view.gain == 0
    assert all(rows[s].action == "on target" for s in ("IVV", "IXUS", "QQQM", "SMH"))
    assert rows["IVV"].invested_target_pct is not None
    assert abs(rows["IVV"].invested_target_pct - D("47.368")) < D("0.001")  # 45/95
    # 95% of the initial 150,930 goes into funds (5% CASH stays cash): 143,383.50.
    # 143,210 bought is within tolerance of it: done, not "partial" for ever.
    assert view.progress[0].fund_target == D("143383.50")
    assert view.progress[0].state == "completed"
    assert view.remaining_to_deploy == D("250930") * D("0.95") - D("143210")


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
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("250930"), datetime(2026, 10, 24))
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
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("250930"), datetime(2026, 10, 24))
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
        IRA, CashFlowKind.DEPOSIT, D("250930"), datetime(2026, 10, 24), "rollover"
    )
    ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    ira.cancel(IRA, 4)
    data = json.loads(json.dumps(export_data(conn)))
    assert data["version"] == 9

    from trd.db.connection import connect

    fresh = connect(tmp_path / "restored.duckdb")
    stats = restore_data(fresh, data)
    assert (stats.cash_flows, stats.alloc_plans) == (1, 1)
    restored = AllocService(fresh, provider).get(IRA)
    assert restored.capital == D("250930")
    assert [t.symbol for t in restored.targets] == ["IVV", "QQQM", "IXUS", CASH, "SMH"]
    assert restored.milestones[4].status == MilestoneStatus.CANCELLED
    assert CashService(fresh).balance(IRA).deposits == D("250930")


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
            "250930",
            "-a",
            IRA,
            "-d",
            "2026-10-24",
            "--note",
            "MassMutual Stable Voyage rollover",
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
            "250930",
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
    assert D(view["cash"]["cash"]) == D("250930")
    assert D(view["total_value"]) == D("250930")
    bad = runner.invoke(app, ["alloc", "edit", "-t", "IVV=50"])
    assert bad.exit_code == 1 and "sum to 100" in bad.output


# ------------------------------------------------------------------ report


def bars(*closes: tuple[date, str]) -> list[DailyBar]:
    return [
        DailyBar(date=on, open=D(c), high=D(c), low=D(c), close=D(c), adj_close=D(c))
        for on, c in closes
    ]


def report_text(ira: AllocService, today: date) -> str:
    from trd.notify.messages import alloc_report_message

    return alloc_report_message(AllocReportService(ira).report(IRA, today=today))


def small_account(conn, provider, ira, ivv_price: str = "606") -> None:
    """$10,000 deposited, $6,000 of it in IVV at 600, the rest cash."""
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 24))
    provider.add_symbol("IVV", price=ivv_price, prev_close="600")
    provider.add_bars("IVV", bars((START, "600"), (date(2026, 10, 27), ivv_price)))
    buy(conn, provider, "IVV", "10", "600")


def test_the_report_leads_with_the_day_and_names_its_returns(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("250930"), datetime(2026, 10, 24))
    ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    provider.add_symbol("IVV", price="606", prev_close="600")
    provider.add_bars("IVV", bars((START, "600"), (date(2026, 10, 27), "606")))
    buy(conn, provider, "IVV", "100", "600")
    text = report_text(ira, date(2026, 10, 27))
    lines = text.splitlines()
    assert lines[0] == "📈 <b>Fidelity Rollover IRA Growth Plan</b> · Tue Oct 27"
    assert lines[2] == "⚪ <b>FLAT DAY  +$600.00 (+0.24%)</b>"
    assert (
        "💰 Worth <b>$251,530.00</b> — up $600.00 (+0.24%) since funding on "
        "$250,930 deposited (simple)"
    ) in lines
    assert "🟢 <b>IVV</b> +1.00% · today +1.00% · $60,600 (100%)" in lines
    assert "⚠️ Off target (±5 pts): IVV +52.6 (overweight)" in text
    assert "🗓 Initial purchase: $150,930 <b>OVERDUE</b> by 1 day(s)" in text
    assert "   $60,000 of $143,384 bought so far" in text
    assert "trd alloc propose" in text
    assert "simple = gain ÷ money put in" in lines[-1]


@pytest.mark.parametrize(
    ("price", "head"),
    [
        ("615", "🚀 <b>GREAT DAY  +$150.00 (+1.50%)</b> 🚀"),
        ("603", "🟢 <b>GOOD DAY  +$30.00 (+0.30%)</b>"),
        ("600.1", "⚪ <b>FLAT DAY  +$1.00 (+0.01%)</b>"),
        ("597.6", "⚪ <b>FLAT DAY  -$24.00 (-0.24%)</b>"),
        ("597", "🔴 <b>DOWN DAY  -$30.00 (-0.30%)</b>"),
        ("585", "🩸 <b>BAD DAY  -$150.00 (-1.50%)</b>"),
    ],
)
def test_the_verdict_is_the_day_as_a_share_of_the_prior_close(
    conn, provider, ira, price, head
) -> None:
    small_account(conn, provider, ira, ivv_price=price)  # prior-day worth is exactly $10,000
    assert report_text(ira, date(2026, 10, 27)).splitlines()[2] == head


@pytest.mark.parametrize(
    ("spy", "line"),
    [
        ("100.2", "Your funds beat the S&P 500 by 0.3 pts today (+0.50% vs +0.20%)."),
        ("100.45", "Your funds were in line with the S&P 500 today (+0.45%)."),
        ("101", "Your funds trailed the S&P 500 by 0.5 pts today (+0.50% vs +1.00%)."),
    ],
)
def test_the_funds_are_put_beside_the_s_and_p_today(conn, provider, ira, spy, line) -> None:
    small_account(conn, provider, ira, ivv_price="603")  # the funds: +0.50% today
    provider.add_symbol("SPY", price=spy, prev_close="100")
    provider.add_bars("SPY", bars((START, "100"), (date(2026, 10, 27), spy)))
    text = report_text(ira, date(2026, 10, 27))
    assert text.splitlines()[3] == line


def test_a_benchmark_buys_the_same_dollars_on_the_same_days(conn, provider, ira) -> None:
    from trd.notify.messages import alloc_report_message, strip_html

    small_account(conn, provider, ira)
    provider.add_symbol("SPY", price="510", prev_close="500")
    # The deposit lands on a Saturday: it buys at the next close, Monday's.
    provider.add_bars("SPY", bars((START, "500"), (date(2026, 10, 27), "510")))
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    spy = next(b for b in report.benchmarks if b.symbol == "SPY")
    assert spy.deposits is not None
    assert (spy.deposits.base, spy.deposits.value) == (D(10000), D(10200))  # 20 sh x 510
    assert (spy.invested.base, spy.invested.value) == (D(6000), D(6120))  # 12 sh x 510
    assert spy.invested.return_pct == 2 and spy.invested.today_pct == 2
    assert report.view.benchmark_value == D(10200)
    text = strip_html(alloc_report_message(report))
    assert f"{'S&P 500':<14} {'+2.00%':>7} {'+2.00%':>7}" in text
    assert "Your funds are behind the S&P 500 by $60 since funding." in text  # 6,060 vs 6,120
    assert "Whole account is behind every deposit put in the S&P 500 by $140." in text
    assert "40% is cash by plan" in text


def test_stored_bars_price_a_benchmark_without_the_provider(conn, provider, ira) -> None:
    from trd.models import InstrumentType
    from trd.repos import InstrumentRepo, PriceRepo

    small_account(conn, provider, ira)
    provider.add_symbol("SPY", price="510", prev_close="500")  # a quote, and no bars at all
    spy = InstrumentRepo(conn).insert(
        InstrumentInfo(symbol="SPY", name="SPY", type=InstrumentType.ETF)
    )
    PriceRepo(conn).upsert_daily(spy.id, bars((START, "500"), (date(2026, 10, 27), "510")))
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    assert report.benchmarks[0].invested.value == D(6120)


def test_the_target_mix_buys_the_plan_s_own_weights(conn, provider, ira) -> None:
    small_account(conn, provider, ira)
    # Invested targets renormalised without CASH: 45/95, 20/95, 25/95, 5/95.
    for symbol, then, now in [("IVV", "600", "606"), ("IXUS", "80", "80"), ("QQQM", "250", "250")]:
        provider.add_symbol(symbol, price=now, prev_close=then)
        provider.add_bars(symbol, bars((START, then)))
    provider.add_symbol("SMH", price="300", prev_close="300")
    provider.add_bars("SMH", bars((START, "300")))
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    mix = next(b for b in report.benchmarks if b.key == "target_mix")
    assert mix.deposits is None
    # 6,000 x 45/95 in IVV gained 1%; the other legs flat.
    assert mix.invested.value is not None
    assert abs(mix.invested.value - (D(6000) + D(6000) * 45 / 95 / 100)) < D("0.0001")


def test_invested_money_is_scored_apart_from_the_cash(conn, provider, ira) -> None:
    small_account(conn, provider, ira)
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    assert (report.invested.base, report.invested.value) == (D(6000), D(6060))
    assert report.invested.return_pct == 1
    assert (report.account.base, report.account.value) == (D(10000), D(10060))
    assert report.account.return_pct == D("0.6")


def test_the_leaderboard_is_best_since_bought_first(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 24))
    provider.add_symbol("IVV", price="606", prev_close="606")
    provider.add_symbol("QQQM", price="240", prev_close="250")
    provider.add_symbol("SMH", price="330", prev_close="300")
    for symbol, then, now in [("IVV", "606", "606"), ("QQQM", "250", "240"), ("SMH", "300", "330")]:
        provider.add_bars(symbol, bars((START, then), (date(2026, 10, 27), now)))
    buy(conn, provider, "IVV", "1", "600")
    buy(conn, provider, "QQQM", "1", "250")
    buy(conn, provider, "SMH", "1", "300")
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    assert [f.symbol for f in report.funds] == ["SMH", "IVV", "QQQM"]
    text = report_text(ira, date(2026, 10, 27))
    order = [
        line.split("<b>")[1].split("</b>")[0] for line in text.splitlines() if " · today " in line
    ]
    assert order == ["SMH", "IVV", "QQQM"]
    assert "🏆 SMH +10.00% today   😬 QQQM -4.00%" in text


def test_a_benchmark_without_a_price_is_a_dash_not_a_zero(conn, provider, ira) -> None:
    from trd.notify.messages import strip_html

    small_account(conn, provider, ira)  # no SPY, QQQ, VT or AOR anywhere
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    assert all(b.invested.value is None for b in report.benchmarks)
    text = strip_html(report_text(ira, date(2026, 10, 27)))
    assert f"{'S&P 500':<14} {'—':>7} {'—':>7}" in text
    assert "ahead of the S&P 500" not in text and "behind the S&P 500" not in text


def test_the_report_escapes_what_it_did_not_write(conn, provider, ira) -> None:
    from trd.notify.messages import strip_html

    small_account(conn, provider, ira)
    ira.edit(IRA, name="Growth & <Moonshot>")
    text = report_text(ira, date(2026, 10, 27))
    assert text.startswith("📈 <b>Growth &amp; &lt;Moonshot&gt;</b>")
    assert "S&amp;P 500" in text and "S&P 500 " not in text.split("<pre>")[1].split("</pre>")[0]
    assert strip_html(text).startswith("📈 Growth & <Moonshot> · Tue Oct 27")


def test_week_and_month_appear_only_once_the_account_is_that_old() -> None:
    from trd.services.alloc_report import period_starts

    today = date(2026, 10, 27)
    assert period_starts(None, today) == {}
    assert period_starts(date(2026, 10, 24), today) == {}
    assert period_starts(date(2026, 10, 20), today) == {"1W": date(2026, 10, 20)}
    assert period_starts(date(2026, 9, 27), today) == {
        "1W": date(2026, 10, 20),
        "1M": date(2026, 9, 27),
    }


def test_a_young_account_reports_no_week(conn, provider, ira) -> None:
    small_account(conn, provider, ira)
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    assert report.funded_on == date(2026, 10, 24)
    assert report.periods == [] and report.account.periods == {}


def test_a_week_old_account_scores_the_week(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 1))
    provider.add_symbol("IVV", price="606", prev_close="606")
    provider.add_bars("IVV", bars((date(2026, 10, 1), "590"), (date(2026, 10, 20), "600")))
    provider.add_symbol("SPY", price="510", prev_close="510")
    provider.add_bars("SPY", bars((date(2026, 10, 1), "490"), (date(2026, 10, 20), "500")))
    buy(conn, provider, "IVV", "10", "590", on=date(2026, 10, 1))
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    assert report.periods == ["1W"]
    assert report.invested.periods["1W"] == 1  # 6,000 at the 10/20 close → 6,060
    # 5,900 bought, 4,100 cash: 10,100 at the 10/20 close → 10,160
    assert report.account.periods["1W"] == D(60) / D(10100) * 100
    spy_week = report.benchmarks[0].invested.periods["1W"]
    assert spy_week is not None and round(spy_week, 9) == 2  # SPY 500 → 510


def test_today_is_measured_from_the_previous_session_close_not_the_quote(
    conn, provider, ira
) -> None:
    """yfinance's prev_close is not always the previous session's close: AOR's
    read 69.00 on 2026-10-23 against a real 68.36, and the report showed -1.25%
    for a -0.32% day. The stored bar wins, for the funds and the benchmarks alike."""
    from trd.models import InstrumentType
    from trd.repos import InstrumentRepo, PriceRepo

    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 24))
    provider.add_symbol("IVV", price="606", prev_close="610")  # the quote says -0.66%
    provider.add_symbol("SPY", price="510", prev_close="520")  # the quote says -1.92%
    buy(conn, provider, "IVV", "10", "600")
    repo, prices = InstrumentRepo(conn), PriceRepo(conn)
    spy = repo.insert(InstrumentInfo(symbol="SPY", name="SPY", type=InstrumentType.ETF))
    ivv = repo.get_by_symbol("IVV")
    assert ivv is not None
    tue = date(2026, 10, 27)
    prices.upsert_daily(ivv.id, bars((START, "600"), (tue, "606")))
    prices.upsert_daily(spy.id, bars((START, "500"), (tue, "510")))
    report = AllocReportService(ira).report(IRA, today=tue)
    assert report.view.day_change == D(60)  # 10 x (606 - 600), not 10 x (606 - 610)
    assert report.funds[0].today_pct == 1
    assert report.invested.today_pct == 1
    assert report.benchmarks[0].invested.today_pct == 2


def test_an_unknown_previous_close_is_unknown_not_the_quote_s(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 24))
    provider.add_symbol("IVV", price="606", prev_close="600")  # a quote, and no bars
    buy(conn, provider, "IVV", "10", "600")
    report = AllocReportService(ira).report(IRA, today=date(2026, 10, 27))
    assert report.view.day_change is None and report.account.today_pct is None
    assert report.funds[0].today_pct is None
    text = report_text(ira, date(2026, 10, 27))
    assert "Can't score today" in text and "· today — ·" in text


def test_a_weekend_report_reads_friday_against_thursday() -> None:
    from trd.services.alloc_report import Closes

    series = Closes({date(2026, 10, 22): D("68.36"), date(2026, 10, 23): D("68.14")})
    assert series.previous_session_close(date(2026, 10, 24)) == D("68.36")  # Saturday
    assert series.previous_session_close(date(2026, 10, 23)) == D("68.36")
    # Bars that stop a week back are stale: no "previous session" from them.
    assert series.previous_session_close(date(2026, 11, 2)) is None


def test_the_report_will_not_print_a_value_built_on_a_missing_price(conn, provider, ira) -> None:
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("10000"), datetime(2026, 10, 24))
    buy(conn, provider, "IVV", "1", "600")
    del provider.quotes["IVV"]
    text = report_text(ira, date(2026, 10, 27))
    assert "⚠️ no price for IVV — worth unknown" in text
    assert "💰 Worth unknown" in text
    assert "since funding" not in text and "vs the market" not in text


def test_the_report_reads_without_writing(cli_env: FakeProvider, monkeypatch) -> None:
    """Unattended on a database that syncs through iCloud: a read-only connection,
    no quote snapshots stored, and the message to the private chat."""
    for symbol, price in [("IVV", "600"), ("IXUS", "80"), ("QQQM", "250"), ("SMH", "300")]:
        cli_env.add_symbol(symbol, price=price, prev_close=price)
    for args in (
        ["init"],
        ["account", "add", IRA],
        ["cash", "deposit", "1000", "-a", IRA, "-d", "2026-10-24"],
        ["alloc", "set", "-a", IRA, "--capital", "1000", "-t", "IVV=95", "-t", "CASH=5"],
        ["buy", "IVV", "1", "--price", "600", "--account", IRA, "--date", "2026-10-26"],
    ):
        assert runner.invoke(app, args).exit_code == 0, args

    import trd.cli.app as cli
    from trd.config import get_settings
    from trd.db.connection import connect

    def snapshots() -> int:
        c = connect(get_settings().db_path)
        try:
            row = c.execute("SELECT count(*) FROM quote_snapshot").fetchone()
            return int(row[0]) if row else 0
        finally:
            c.close()

    before = snapshots()
    sent: list[str] = []

    class Fake:
        def send(self, text: str, html: bool = False) -> None:
            assert html  # the message is Telegram HTML
            sent.append(text)

    monkeypatch.setattr(cli, "alert_notifier_from_env", lambda **_: Fake())
    result = runner.invoke(app, ["alloc", "report", "--notify", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sent"] is True and sent == [payload["message"]]
    assert {"view", "report"} <= payload.keys() and "benchmarks" in payload["report"]
    assert snapshots() == before

    monkeypatch.setattr(cli, "alert_notifier_from_env", lambda **_: None)
    refused = runner.invoke(app, ["alloc", "report", "--notify"])
    assert refused.exit_code == 1
    plain = runner.invoke(app, ["alloc", "report"])
    assert plain.exit_code == 0 and "<b>" not in plain.output and "vs the market" in plain.output


# ------------------------------------------------------------------ paced (SB-1388)

MOONSHOT = [
    AllocTarget(symbol="IVV", weight=D(43)),
    AllocTarget(symbol="IXUS", weight=D(20)),
    AllocTarget(symbol="QQQM", weight=D(24)),
    AllocTarget(symbol="SMH", weight=D(5)),
    AllocTarget(symbol="SPCX", weight=D(3), pace_months=24, pace_start=START),
    AllocTarget(symbol=CASH, weight=D(5)),
]


@pytest.fixture
def moonshot(conn, provider, ira) -> AllocService:
    provider.add_symbol("SPCX", price="150", prev_close="150")
    ira.edit(IRA, targets=MOONSHOT)
    CashService(conn).record(IRA, CashFlowKind.DEPOSIT, D("250930"), datetime(2026, 10, 24))
    ira.schedule(IRA, START, D(150930), D(25000), 3, 4)
    return ira


def test_instalments_sum_to_the_earmark_exactly() -> None:
    from trd.services.alloc import instalments

    rows = instalments(D("7527.90"), 24, START)
    assert rows[0] == (START, D("313.66")) and rows[1][0] == date(2026, 11, 26)
    assert rows[-1] == (date(2028, 9, 26), D("313.72"))
    assert sum(a for _, a in rows) == D("7527.90")


def test_cash_cannot_be_paced() -> None:
    bad = [*TARGETS[:4], AllocTarget(symbol=CASH, weight=D(5), pace_months=12, pace_start=START)]
    with pytest.raises(TrdError, match="CASH cannot be paced"):
        validate_plan(D(1000), bad, D(5))


def test_a_paced_target_is_earmarked_cash_not_double_counted(conn, provider, moonshot) -> None:
    view = moonshot.view(IRA, today=START)
    (row,) = view.paced
    assert (row.earmark, row.instalment, row.instalments_due) == (D("7527.90"), D("313.66"), 1)
    assert (row.behind, row.reserved, row.next_date) == (
        D("313.66"),
        D("7527.90"),
        date(2026, 11, 26),
    )
    assert view.reserved_cash == D("7527.90")
    assert view.total_value == D("250930")  # reserved is part of cash, not added to it
    assert view.plan.fund_fraction == D("0.92")


def test_paced_buys_count_toward_the_earmark_not_the_milestones(conn, provider, moonshot) -> None:
    buy(conn, provider, "SPCX", "2", "150")  # 300 of the first 313.66 instalment
    buy(conn, provider, "IVV", "100", "600")
    view = moonshot.view(IRA, today=START)
    (row,) = view.paced
    assert (row.invested, row.behind, row.reserved) == (D(300), D("13.66"), D("7227.90"))
    assert view.deployed == D(60000)  # SPCX is not tranche money
    initial = view.progress[0]
    assert (initial.fund_target, initial.invested_toward) == (D("138855.60"), D(60000))
    ivv = next(r for r in view.rows if r.symbol == "IVV")
    spcx = next(r for r in view.rows if r.symbol == "SPCX")
    assert ivv.invested_pct == 100  # the drift sleeve leaves the moonshot out
    assert spcx.invested_target_pct is None and spcx.drift_pp is None


def test_a_proposal_holds_the_paced_share_back_and_lists_what_is_due(
    conn, provider, moonshot
) -> None:
    p = moonshot.proposal(IRA, mode="target", today=START)
    assert p.amount == D("150930.00")
    assert (p.held_as_cash, p.held_for_paced) == (D("7546.50"), D("4527.90"))
    assert sum(leg.amount for leg in p.legs) == D("138855.60")
    assert "SPCX" not in {leg.symbol for leg in p.legs}
    (due,) = p.paced_due
    assert (due.symbol, due.amount, due.shares) == ("SPCX", D("313.66"), D("2.091"))


def test_the_report_shows_the_moonshot_and_reserved_cash(conn, provider, moonshot) -> None:
    from trd.notify.messages import alloc_report_message

    buy(conn, provider, "SPCX", "2", "150")
    provider.add_bars("SPCX", bars((date(2026, 10, 23), "150"), (START, "150")))
    text = alloc_report_message(AllocReportService(moonshot).report(IRA, today=START))
    assert " · $7,228 set aside for SPCX" in text
    assert "🚀 <b>SPCX</b> +0.00% · today +0.00% · $300 (100%)" in text
    assert "🚀 SPCX: 0 of 24 buys done · buy $13.66 now" in text


def test_a_few_cents_short_of_an_instalment_is_on_schedule(conn, provider, moonshot) -> None:
    from trd.notify.messages import alloc_report_message

    buy(conn, provider, "SPCX", "2.0910", "150")  # 313.65 of a 313.66 instalment
    report = AllocReportService(moonshot).report(IRA, today=START)
    assert report.view.paced[0].behind == 0
    text = alloc_report_message(report)
    assert "🚀 SPCX: 1 of 24 buys done · next $313.66 on Nov 26" in text


def test_pace_survives_backup_and_restore(conn, provider, moonshot, tmp_path) -> None:
    from trd.db.connection import connect

    data = json.loads(json.dumps(export_data(conn)))
    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    spcx = next(t for t in AllocService(fresh, provider).get(IRA).targets if t.symbol == "SPCX")
    assert (spcx.pace_months, spcx.pace_start) == (24, START)


def test_cli_pace_re_paces_existing_targets(cli_env: FakeProvider) -> None:
    for symbol, price in [("IVV", "600"), ("IXUS", "80"), ("QQQM", "250"), ("SMH", "300")]:
        cli_env.add_symbol(symbol, price=price, prev_close=price)
    cli_env.add_symbol("SPCX", price="150", prev_close="150")
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["account", "add", IRA]).exit_code == 0
    targets = [x for t in MOONSHOT for x in ("-t", f"{t.symbol}={t.weight}")]
    r = runner.invoke(app, ["alloc", "set", "-a", IRA, "--capital", "250930", *targets])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["alloc", "edit", "--pace", "SPCX=24", "--pace-start", "2026-10-26"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["alloc", "history"])
    assert "SPCX=3 (over 24 months from 2026-10-26)" in r.output
    r = runner.invoke(app, ["alloc", "edit", "--pace", "IVV=x"])
    assert r.exit_code != 0
