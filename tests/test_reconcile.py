"""Broker vs trd: the gap no backtest shows.

A paper book can be perfectly consistent with itself and still describe a
portfolio that does not exist. This is the only command that says so.

The comparison is deliberately offline — the broker read happens in an
authenticated MCP session and lands in a snapshot file — which is what makes
every case below testable without a brokerage account.
"""

from datetime import date, datetime
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.errors import UnknownAccountError
from trd.models import (
    AccountType,
    BrokerPosition,
    BrokerSnapshot,
    ReconcileStatus,
    Side,
)
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo
from trd.services import PortfolioService, ReconcileService

AS_OF = datetime(2026, 8, 3, 14, 22)


@pytest.fixture
def books(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> PortfolioService:
    """An account holding 10 AAPL and 4 NVDA, bought then partly sold."""
    provider.add_symbol("AAPL", price="200")
    provider.add_symbol("NVDA", price="120")
    AccountRepo(conn).create("rh-agent", AccountType.REAL)
    service = PortfolioService(conn, provider)
    service.record_trade("rh-agent", "AAPL", Side.BUY, Decimal("10"), Decimal("190"))
    service.record_trade("rh-agent", "NVDA", Side.BUY, Decimal("6"), Decimal("100"))
    service.record_trade("rh-agent", "NVDA", Side.SELL, Decimal("2"), Decimal("125"))
    return service


@pytest.fixture
def reconcile(conn: duckdb.DuckDBPyConnection) -> ReconcileService:
    return ReconcileService(conn)


def snapshot(*positions: tuple[str, str], cash: str | None = None) -> BrokerSnapshot:
    return BrokerSnapshot(
        as_of=AS_OF,
        positions=[BrokerPosition(symbol=s, quantity=Decimal(q)) for s, q in positions],
        cash=Decimal(cash) if cash else None,
    )


# ------------------------------------------------------------------- the four verdicts


def test_matching_books_are_in_sync(books, reconcile) -> None:
    result = reconcile.reconcile(snapshot(("AAPL", "10"), ("NVDA", "4")), "rh-agent")
    assert result.in_sync
    assert result.mismatches == 0
    assert [r.status for r in result.rows] == [ReconcileStatus.OK, ReconcileStatus.OK]


def test_a_different_share_count_is_reported_with_its_delta(books, reconcile) -> None:
    result = reconcile.reconcile(snapshot(("AAPL", "9"), ("NVDA", "4")), "rh-agent")
    assert not result.in_sync
    row = result.rows[0]
    assert row.symbol == "AAPL"
    assert row.status == ReconcileStatus.QUANTITY
    assert row.quantity_delta == Decimal("-1")  # broker holds one less


def test_a_position_the_broker_does_not_have(books, reconcile) -> None:
    """The dangerous one: trd believes it owns something it does not."""
    result = reconcile.reconcile(snapshot(("AAPL", "10")), "rh-agent")
    row = next(r for r in result.rows if r.symbol == "NVDA")
    assert row.status == ReconcileStatus.MISSING_AT_BROKER
    assert row.broker_quantity is None
    assert row.trd_quantity == Decimal("4")
    assert row.quantity_delta is None  # no arithmetic across a missing side


def test_a_position_trd_has_never_heard_of(books, reconcile) -> None:
    result = reconcile.reconcile(
        snapshot(("AAPL", "10"), ("NVDA", "4"), ("SOFI", "12")), "rh-agent"
    )
    row = next(r for r in result.rows if r.symbol == "SOFI")
    assert row.status == ReconcileStatus.UNTRACKED
    assert row.trd_quantity is None


# ------------------------------------------------------------------ the careful parts


def test_a_fully_exited_position_is_not_a_holding(books, reconcile) -> None:
    """Selling out must not leave a ghost. Otherwise every closed trade would
    report MISSING AT BROKER forever, against a broker that is entirely right."""
    books.record_trade("rh-agent", "NVDA", Side.SELL, Decimal("4"), Decimal("130"))
    result = reconcile.reconcile(snapshot(("AAPL", "10")), "rh-agent")
    assert [r.symbol for r in result.rows] == ["AAPL"]
    assert result.in_sync


def test_fractional_rounding_is_not_a_mismatch(books, conn, reconcile) -> None:
    """Share counts are stored to 6 decimals. A difference in the 8th place is a
    representation artifact, and flagging it would make the command cry wolf on a
    book that is fine."""
    result = reconcile.reconcile(snapshot(("AAPL", "10.0000001"), ("NVDA", "4")), "rh-agent")
    assert result.in_sync

    # A real fractional break still lands.
    off = reconcile.reconcile(snapshot(("AAPL", "10.001"), ("NVDA", "4")), "rh-agent")
    assert not off.in_sync


def test_problems_sort_above_matches(books, reconcile) -> None:
    """The reason to run this is to find trouble; a clean book must not scroll
    it off the top."""
    result = reconcile.reconcile(snapshot(("AAPL", "10"), ("NVDA", "1")), "rh-agent")
    assert result.rows[0].symbol == "NVDA"
    assert result.rows[0].status == ReconcileStatus.QUANTITY
    assert result.rows[1].status == ReconcileStatus.OK


def test_only_the_named_account_is_compared(books, conn, provider, reconcile) -> None:
    """FIFO runs per account. A holding in another account is not this broker's
    business, and counting it would invent a mismatch."""
    AccountRepo(conn).create("other", AccountType.REAL)
    books.record_trade("other", "AAPL", Side.BUY, Decimal("50"), Decimal("190"))
    result = reconcile.reconcile(snapshot(("AAPL", "10"), ("NVDA", "4")), "rh-agent")
    assert result.in_sync


def test_an_unknown_account_is_an_error_not_an_empty_diff(books, reconcile) -> None:
    """Silently reconciling against nothing would report every broker position as
    UNTRACKED and look like a catastrophe."""
    with pytest.raises(UnknownAccountError):
        reconcile.reconcile(snapshot(("AAPL", "10")), "nope")


# ------------------------------------------------------------------------ price gaps


def test_the_stored_close_comes_back_with_its_date(books, conn, reconcile) -> None:
    """A price 4% from the broker's is a stale bar if the close is three days old
    and a real disagreement if it is today's. Only the date tells them apart."""
    from tests.test_engine import make_bars

    instrument = InstrumentRepo(conn).get_by_symbol("AAPL")
    assert instrument is not None
    bars = make_bars([100.0, 210.0])
    PriceRepo(conn).upsert_daily(instrument.id, bars)

    snap = BrokerSnapshot(
        as_of=AS_OF,
        positions=[
            BrokerPosition(symbol="AAPL", quantity=Decimal("10"), price=Decimal("200")),
            BrokerPosition(symbol="NVDA", quantity=Decimal("4")),
        ],
    )
    row = next(r for r in reconcile.reconcile(snap, "rh-agent").rows if r.symbol == "AAPL")
    assert row.trd_price == Decimal("210")
    assert row.trd_price_date == bars[-1].date
    assert row.price_delta_pct == Decimal("5")  # trd's mark is 5% above the broker's


def test_no_price_on_either_side_is_not_a_gap(books, reconcile) -> None:
    """A snapshot without prices compares share counts and says nothing about
    marks, rather than inventing a delta against zero."""
    row = reconcile.reconcile(snapshot(("AAPL", "10"), ("NVDA", "4")), "rh-agent").rows[0]
    assert row.broker_price is None
    assert row.price_delta_pct is None


# ---------------------------------------------------------------------------- cash


def test_broker_cash_is_carried_but_never_diffed(books, reconcile) -> None:
    """trd has no cash ledger. Reporting the broker's number is useful; inventing
    a delta for it would not be."""
    snap = snapshot(("AAPL", "10"), ("NVDA", "4"), cash="1204.11")
    result = reconcile.reconcile(snap, "rh-agent")
    assert result.broker_cash == Decimal("1204.11")


# -------------------------------------------------------------------- shape of output


def test_symbols_are_normalised_before_comparison(books, reconcile) -> None:
    """A broker that says 'aapl' holds the same thing trd calls 'AAPL'."""
    result = reconcile.reconcile(snapshot(("aapl", "10"), (" nvda ", "4")), "rh-agent")
    assert result.in_sync


def test_the_verdict_survives_json(books, reconcile) -> None:
    result = reconcile.reconcile(snapshot(("AAPL", "9")), "rh-agent")
    dumped = result.model_dump()
    for key in ("in_sync", "mismatches", "symbols_compared"):
        assert key in dumped, f"{key} missing from Reconciliation JSON"
    for key in ("quantity_delta", "price_delta_pct", "matched"):
        assert key in dumped["rows"][0], f"{key} missing from ReconcileRow JSON"


def test_the_snapshot_format_parses_as_documented() -> None:
    """The shape in docs/robinhood-mcp.md, verbatim. If this drifts, whatever
    writes snapshots is writing them against a format that no longer loads."""
    parsed = BrokerSnapshot.model_validate_json(
        """
        {
          "as_of": "2026-08-03T14:22:00-04:00",
          "source": "robinhood",
          "account": "rh-agent",
          "cash": "1204.11",
          "positions": [
            {"symbol": "AMZN", "quantity": "3.759964", "price": "284.56"},
            {"symbol": "MU",   "quantity": "1.263104", "price": "796.21"}
          ]
        }
        """
    )
    assert parsed.source == "robinhood"
    assert parsed.cash == Decimal("1204.11")
    assert [p.symbol for p in parsed.positions] == ["AMZN", "MU"]
    assert parsed.positions[0].quantity == Decimal("3.759964")
    assert parsed.as_of.date() == date(2026, 8, 3)


# --------------------------------------------------------------------------- the CLI


def test_cli_reconcile_reports_and_exits_non_zero(cli_env, tmp_path) -> None:
    """Exit code is the contract for a scheduled check: a disagreeing book must
    fail, not print a warning nobody reads."""
    import json

    from typer.testing import CliRunner

    from trd.cli.app import app

    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["buy", "AAPL", "10", "--price", "150"]).exit_code == 0

    path = tmp_path / "broker.json"
    path.write_text(
        json.dumps(
            {
                "as_of": "2026-08-03T14:22:00",
                "positions": [{"symbol": "AAPL", "quantity": "9"}],
            }
        )
    )

    result = runner.invoke(app, ["engine", "reconcile", str(path), "--account", "main"])
    assert result.exit_code == 1, result.output
    assert "QUANTITY" in result.output

    matched = tmp_path / "ok.json"
    matched.write_text(
        json.dumps(
            {
                "as_of": "2026-08-03T14:22:00",
                "positions": [{"symbol": "AAPL", "quantity": "10"}],
            }
        )
    )
    ok = runner.invoke(app, ["engine", "reconcile", str(matched), "--account", "main"])
    assert ok.exit_code == 0, ok.output
    assert "in sync" in ok.output


def test_cli_reconcile_rejects_a_file_that_is_not_a_snapshot(cli_env, tmp_path) -> None:
    """A malformed snapshot must fail loudly. Parsing it as an empty book would
    report every real holding as MISSING AT BROKER — a false catastrophe."""
    from typer.testing import CliRunner

    from trd.cli.app import app

    runner = CliRunner()
    runner.invoke(app, ["init"])
    path = tmp_path / "junk.json"
    path.write_text('{"holdings": []}')
    result = runner.invoke(app, ["engine", "reconcile", str(path), "--account", "main"])
    assert result.exit_code == 1
    assert "not a broker snapshot" in result.output


def test_cli_reconcile_json_is_one_document(cli_env, tmp_path) -> None:
    import json

    from typer.testing import CliRunner

    from trd.cli.app import app

    runner = CliRunner()
    runner.invoke(app, ["init"])
    runner.invoke(app, ["buy", "AAPL", "10", "--price", "150"])
    path = tmp_path / "broker.json"
    path.write_text(
        json.dumps(
            {"as_of": "2026-08-03T14:22:00", "positions": [{"symbol": "AAPL", "quantity": "10"}]}
        )
    )
    result = runner.invoke(app, ["engine", "reconcile", str(path), "--account", "main", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert doc["in_sync"] is True
    assert doc["rows"][0]["symbol"] == "AAPL"
