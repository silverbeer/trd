"""Monthly IRA head-to-head (SB-1420). Synthetic accounts, public tickers,
invented prices.

What is worth pinning: a fee lowers the after-fee return and "before fees" is
the return without it; deposits are not gains; benchmarks are the same dollars
on the same days on dividend-adjusted closes while accounts use real closes;
ties share the lead; an out-of-date managed account is shown but kept out of
the verdict; and nothing but labels reaches the message.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from trd.cli.app import app
from trd.models import AccountType, DailyBar, Side
from trd.models.alloc import CashFlowKind
from trd.notify.messages import ira_compare_message
from trd.notify.privacy import guard
from trd.repos import AccountRepo
from trd.repos.alloc import CashFlowRepo
from trd.repos.instruments import InstrumentRepo
from trd.repos.transactions import TransactionRepo
from trd.services.alloc_report import Closes
from trd.services.ira_compare import (
    IraCompareService,
    basket_value,
    cash_on,
    dietz,
    month_window,
)

D = Decimal
TODAY = date(2026, 12, 3)


def at(on: date) -> datetime:
    return datetime(on.year, on.month, on.day, 16, 0)


def flat_bars(start: date, end: date, close: str, adj: str | None = None) -> list[DailyBar]:
    c = D(close)
    out: list[DailyBar] = []
    on = start
    while on <= end:
        if on.weekday() < 5:
            out.append(
                DailyBar(date=on, open=c, high=c, low=c, close=c, adj_close=D(adj) if adj else None)
            )
        on += timedelta(days=1)
    return out


# ------------------------------------------------------------------ the maths


def test_dietz_without_flows_is_the_simple_return() -> None:
    assert dietz(D(100), D(110), [], date(2026, 11, 1), date(2026, 11, 30)) == D(10)


def test_dietz_does_not_count_a_deposit_as_gain() -> None:
    start, end = month_window(2026, 11)
    # 100 grows to 105, plus 100 deposited on the last day: 5% on 100, not 105%.
    r = dietz(D(100), D(205), [(end, D(100))], start, end)
    assert r is not None and abs(r - D("4.839")) < D("0.001")  # 5 / (100 + 100/30)


def test_dietz_with_nothing_invested_is_unknown() -> None:
    assert dietz(D(0), D(0), [], date(2026, 11, 1), date(2026, 11, 30)) is None


def test_cash_follows_deposits_fees_trades_and_income() -> None:
    from trd.models import Transaction
    from trd.models.alloc import CashFlow
    from trd.models.core import Income

    day = date(2026, 11, 2)
    flows = [
        CashFlow(
            id=1, account_id=1, kind=CashFlowKind.DEPOSIT, amount=D(1000), occurred_at=at(day)
        ),
        CashFlow(id=2, account_id=1, kind=CashFlowKind.FEE, amount=D(10), occurred_at=at(day)),
    ]
    income = [Income(id=1, account_id=1, amount=D(5), received_at=at(day))]
    txns = [
        Transaction(
            id=1,
            account_id=1,
            instrument_id=1,
            side=Side.BUY,
            quantity=D(2),
            price=D(100),
            fees=D(1),
            executed_at=at(day),
        )
    ]
    assert cash_on(day, flows, income, txns) == D(1000 - 10 + 5 - 201)
    assert cash_on(day - timedelta(days=1), flows, income, txns) == D(0)


def test_benchmark_buys_the_same_dollars_on_their_day() -> None:
    start, end = month_window(2026, 11)
    closes = {
        "SPY": Closes({start - timedelta(days=3): D(100), date(2026, 11, 16): D(200), end: D(300)})
    }
    # 100 at the close before the window (100) = 1 share; 400 on the 16th (200) = 2 shares.
    value = basket_value(D(100), [(date(2026, 11, 16), D(400))], {"SPY": D(1)}, closes, start, end)
    assert value == D(900)


# ------------------------------------------------------------------ the service


def _account(
    conn: duckdb.DuckDBPyConnection,
    name: str,
    *,
    deposit: date,
    amount: str = "1000",
    fee: tuple[date, str] | None = None,
    shares: str = "10",
    imported: datetime | None = None,
) -> None:
    account = AccountRepo(conn).create(name, AccountType.REAL)
    instrument = InstrumentRepo(conn).get_by_symbol("VTI")
    assert instrument is not None
    CashFlowRepo(conn).add(account.id, CashFlowKind.DEPOSIT, D(amount), at(deposit))
    TransactionRepo(conn).insert(
        account.id, instrument.id, Side.BUY, D(shares), D(100), D(0), at(deposit)
    )
    if fee is not None:
        CashFlowRepo(conn).add(account.id, CashFlowKind.FEE, D(fee[1]), at(fee[0]))
    if imported is not None:
        conn.execute(
            "INSERT INTO import_row (account_id, source, row_key, imported_at)"
            " VALUES (?, 'x', 'k', ?)",
            [account.id, imported],
        )


@pytest.fixture
def market(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> FakeProvider:
    """VTI flat at 100 until Nov 30, 110 from Dec 1 on. SPY real closes flat but
    adjusted closes up 1% in November (a dividend); AGG flat."""
    first = date(2025, 10, 1)
    provider.add_bars(
        "VTI",
        flat_bars(first, date(2026, 11, 29), "100") + flat_bars(date(2026, 11, 30), TODAY, "110"),
    )
    provider.add_bars(
        "SPY",
        flat_bars(first, date(2026, 10, 31), "500", "495")
        + flat_bars(date(2026, 11, 1), TODAY, "500", "499.95"),
    )
    provider.add_bars("AGG", flat_bars(first, TODAY, "100"))
    from trd.models import InstrumentInfo, InstrumentType

    InstrumentRepo(conn).insert(
        InstrumentInfo(symbol="VTI", name="VTI", type=InstrumentType.ETF, currency="USD")
    )
    return provider


def test_fees_lower_the_return_and_before_fees_adds_them_back(
    conn: duckdb.DuckDBPyConnection, market: FakeProvider
) -> None:
    _account(conn, "self", deposit=date(2026, 10, 1))
    _account(
        conn,
        "managed",
        deposit=date(2026, 10, 1),
        fee=(date(2026, 11, 13), "10"),
        imported=datetime(2026, 12, 2, 9, 0),
    )
    service = IraCompareService(conn, market)
    r = service.report([("self", "Self"), ("managed", "Mgd")], 2026, 11, date(2026, 10, 2), TODAY)
    me, them = r.accounts
    assert me.month.return_pct == D(10)  # 10 shares 100 -> 110, cash 0
    assert them.month.fees == D(10)
    assert them.month.return_pct == D(9)  # the same, less a $10 fee on $1,000
    # The fee handed back as a withdrawal on its day: 100 / (1000 - 10 * 18/30).
    gross = them.month.before_fees_pct
    assert gross is not None and abs(gross - D("10.060")) < D("0.001")
    assert them.fees is not None and them.fees.month == D(10) and them.fees.ytd == D(10)
    assert me.fees is None
    # SPY's adjusted close rose 1% in November: the benchmark carries the dividend.
    bench = me.month.benchmarks["sp500"]
    assert bench is not None and abs(bench - D(1)) < D("0.01")
    assert r.leaders_month == ["Self"]


def test_a_tie_shares_the_lead(conn: duckdb.DuckDBPyConnection, market: FakeProvider) -> None:
    _account(conn, "a", deposit=date(2026, 10, 1))
    _account(conn, "b", deposit=date(2026, 10, 1))
    r = IraCompareService(conn, market).report(
        [("a", "A"), ("b", "B")], 2026, 11, date(2026, 10, 2), TODAY
    )
    assert r.leaders_month == ["A", "B"]


def test_a_stale_import_is_shown_but_kept_out_of_the_verdict(
    conn: duckdb.DuckDBPyConnection, market: FakeProvider
) -> None:
    _account(conn, "self", deposit=date(2026, 10, 1))
    _account(conn, "old", deposit=date(2026, 10, 1), imported=datetime(2026, 11, 20, 9, 0))
    _account(conn, "new", deposit=date(2026, 10, 1), shares="5", imported=datetime(2026, 12, 2))
    r = IraCompareService(conn, market).report(
        [("self", "Self"), ("old", "Old"), ("new", "New")], 2026, 11, date(2026, 10, 2), TODAY
    )
    old = next(a for a in r.accounts if a.label == "Old")
    assert not old.fresh and old.month.return_pct is not None
    assert "Old" not in r.leaders_month
    text = ira_compare_message(r)
    assert "⏳" in text and "not in the verdict" in text


def test_a_month_not_over_is_valued_through_yesterday(
    conn: duckdb.DuckDBPyConnection, market: FakeProvider
) -> None:
    _account(conn, "a", deposit=date(2026, 10, 1))
    _account(conn, "b", deposit=date(2026, 10, 1))
    r = IraCompareService(conn, market).report(
        [("a", "A"), ("b", "B")], 2026, 12, date(2026, 10, 2), TODAY
    )
    assert r.partial and r.month_end == TODAY - timedelta(days=1)


def test_common_start_is_the_latest_first_deposit(
    conn: duckdb.DuckDBPyConnection, market: FakeProvider
) -> None:
    _account(conn, "early", deposit=date(2025, 11, 18))
    _account(conn, "late", deposit=date(2026, 10, 23))
    assert IraCompareService(conn, market).common_start(["early", "late"]) == date(2026, 10, 23)


def test_message_carries_labels_never_account_names(
    conn: duckdb.DuckDBPyConnection, market: FakeProvider
) -> None:
    _account(conn, "secret-name-one", deposit=date(2026, 10, 1))
    _account(
        conn,
        "secret-name-two",
        deposit=date(2026, 10, 1),
        fee=(date(2026, 11, 13), "10"),
        imported=datetime(2026, 12, 2),
    )
    r = IraCompareService(conn, market).report(
        [("secret-name-one", "Self"), ("secret-name-two", "Mgd")],
        2026,
        11,
        date(2026, 10, 2),
        TODAY,
    )
    text = ira_compare_message(r)
    assert "secret-name" not in text
    assert "Self" in text and "Advisory fees" in text and "S&amp;P 500" in text
    guard(text, env={})  # the send guard passes it


# ------------------------------------------------------------------ the CLI


def test_cli_needs_two_labelled_accounts(
    cli_env: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRD_IRA_COMPARE", raising=False)
    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    result = runner.invoke(app, ["alloc", "compare", "--account", "main=Main"])
    assert result.exit_code == 1 and "two or more" in result.output
    result = runner.invoke(app, ["alloc", "compare", "--account", "main", "--account", "x=y"])
    assert result.exit_code == 1 and "NAME=LABEL" in result.output
