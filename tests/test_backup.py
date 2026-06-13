from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from tests.conftest import FakeProvider
from trd.db.connection import connect
from trd.errors import TrdError
from trd.models import AccountType, Side
from trd.services import PlanService, PortfolioService, WatchlistService
from trd.services.backup import export_data, restore_data
from trd.services.indicators import seed_defaults


def _populate(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> None:
    from trd.models import InstrumentType

    provider.add_symbol("SPY", price="500.00", type_=InstrumentType.ETF)
    provider.add_symbol("QQQ", price="200.00", type_=InstrumentType.ETF)
    seed_defaults(conn)
    portfolio = PortfolioService(conn, provider)
    portfolio.accounts.create("main", AccountType.REAL)
    portfolio.accounts.create("fidelity", AccountType.REAL)
    portfolio.record_trade(
        "fidelity", "AAPL", Side.BUY, Decimal(10), Decimal(150), executed_at=datetime(2025, 1, 5)
    )
    portfolio.record_trade(
        "main", "NVDA", Side.BUY, Decimal(2), Decimal(100), executed_at=datetime(2025, 2, 1)
    )
    plans = PlanService(conn, provider)
    plans.set_plan(
        "sim",
        Decimal(100),
        create_simulation=True,
        allocations={"SPY": Decimal(40), "QQQ": Decimal(60)},
        day_of_month=15,
        note="paper experiment",
    )
    plans.invest("sim")  # live price — no bars needed
    WatchlistService(conn, provider).add("NVDA", "ai")


def test_round_trip(tmp_path: Path, conn: duckdb.DuckDBPyConnection, provider) -> None:
    _populate(conn, provider)
    data = export_data(conn)

    # restore into a fresh DB
    fresh = connect(tmp_path / "restored.duckdb")
    stats = restore_data(fresh, data)
    assert stats.transactions == 4  # AAPL, NVDA, + 2 sim allocation legs
    assert stats.accounts == 3  # main, fidelity, sim

    # transactions preserved with account + symbol + dates
    rows = fresh.execute(
        """SELECT a.name, i.symbol, t.side, t.quantity, t.price, t.executed_at, t.plan_id
           FROM txn t JOIN account a ON a.id=t.account_id JOIN instrument i ON i.id=t.instrument_id
           ORDER BY t.executed_at, i.symbol"""
    ).fetchall()
    assert ("fidelity", "AAPL", "buy", Decimal(10), Decimal(150)) == rows[0][:5]
    assert rows[0][5].date().isoformat() == "2025-01-05"

    # plan + allocation restored and re-linked to its account's txns
    plans = PlanService(fresh, provider)
    plan = plans.get_plan("sim")
    assert plan.day_of_month == 15
    assert plan.allocations == {"QQQ": Decimal(60), "SPY": Decimal(40)}
    sim_txns = plans.txns.list_for_plan(plan.id)
    assert len(sim_txns) == 2  # the two allocation legs carry the plan_id

    # watchlist + indicators restored
    wl = WatchlistService(fresh, provider)
    assert [i.symbol for _, i in wl.watchlists.items()] == ["NVDA"]
    from trd.repos import IndicatorConfigRepo

    assert IndicatorConfigRepo(fresh).count() == 9


def test_restore_refuses_without_force(
    tmp_path: Path, conn: duckdb.DuckDBPyConnection, provider
) -> None:
    _populate(conn, provider)
    data = export_data(conn)
    target = connect(tmp_path / "t.duckdb")
    PortfolioService(target, provider).accounts.create("existing", AccountType.REAL)
    PortfolioService(target, provider).record_trade(
        "existing", "NVDA", Side.BUY, Decimal(1), Decimal(100)
    )
    with pytest.raises(TrdError, match="--force"):
        restore_data(target, data)


def test_cli_backup_restore_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider
) -> None:
    from typer.testing import CliRunner

    import trd.cli.app as cli
    from trd.cli.app import app

    runner = CliRunner()
    home = tmp_path / "home"
    monkeypatch.setenv("TRD_HOME", str(home))
    monkeypatch.setattr(cli, "YFinanceProvider", lambda: provider)

    # build a DB, back it up
    src_conn = connect(home / "trd.duckdb")
    _populate(src_conn, provider)
    src_conn.close()
    backup_file = tmp_path / "backup.json"
    assert runner.invoke(app, ["backup", str(backup_file)]).exit_code == 0

    # a fresh machine: empty DB → restore loads it
    home2 = tmp_path / "home2"
    monkeypatch.setenv("TRD_HOME", str(home2))
    result = runner.invoke(app, ["restore", str(backup_file)])
    assert result.exit_code == 0, result.output
    assert "Restored" in result.output

    # restoring again without --force is refused; with --force it rebuilds
    assert runner.invoke(app, ["restore", str(backup_file)]).exit_code == 1
    forced = runner.invoke(app, ["restore", str(backup_file), "--force"])
    assert forced.exit_code == 0, forced.output


def test_version_mismatch_rejected(conn: duckdb.DuckDBPyConnection) -> None:
    with pytest.raises(TrdError, match="version"):
        restore_data(conn, {"version": 999})
