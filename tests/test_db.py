import json
from pathlib import Path

import pytest

from trd.db.connection import (
    MIGRATIONS_DIR,
    apply_migrations,
    connect,
    connect_read_only,
    pending_migrations,
)
from trd.errors import SchemaBehindError

EXPECTED_TABLES = {"instrument", "account", "txn", "price_daily", "quote_snapshot"}


def test_migrations_create_tables(tmp_path: Path) -> None:
    conn = connect(tmp_path / "t.duckdb")
    tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
    assert tables >= EXPECTED_TABLES


def test_migrations_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "t.duckdb"
    conn = connect(db)
    assert apply_migrations(conn) == []  # second pass applies nothing
    conn.close()
    conn2 = connect(db)  # reopen runs migrations again, harmlessly
    row = conn2.execute("SELECT count(*) FROM schema_migrations").fetchone()
    assert row is not None and row[0] >= 1


# ------------------------------------------------------- read-only (SB-1453)


def _behind(db: Path, *names: str) -> None:
    """A database as an older build left it: these migrations never recorded."""
    conn = connect(db)
    for name in names:
        conn.execute("DELETE FROM schema_migrations WHERE filename = ?", [name])
    conn.close()


def test_read_only_opens_a_current_database(tmp_path: Path) -> None:
    db = tmp_path / "t.duckdb"
    connect(db).close()
    conn = connect_read_only(db)
    assert pending_migrations(conn) == []
    conn.close()


def test_read_only_refuses_a_database_behind_this_build(tmp_path: Path) -> None:
    """Refused on open with the fix named, never a binder error mid-query."""
    db = tmp_path / "t.duckdb"
    newest = sorted(MIGRATIONS_DIR.glob("*.sql"))[-1].name
    _behind(db, newest)
    with pytest.raises(SchemaBehindError, match="trd engine status") as caught:
        connect_read_only(db)
    assert caught.value.pending == [newest]
    assert "1 migration pending" in str(caught.value)


def test_a_migration_only_a_newer_build_knows_is_not_pending(tmp_path: Path) -> None:
    db = tmp_path / "t.duckdb"
    conn = connect(db)
    conn.execute("INSERT INTO schema_migrations (filename) VALUES ('999_from_the_future.sql')")
    conn.close()
    ro = connect_read_only(db)
    assert pending_migrations(ro) == []
    ro.close()


def test_read_only_cli_reports_it_cleanly(cli_env, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from trd.cli.app import app
    from trd.config import get_settings

    runner = CliRunner()
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert (
        runner.invoke(app, ["engine", "init", "--symbols", "AAA", "--capital", "100"]).exit_code
        == 0
    )
    _behind(get_settings().db_path, sorted(MIGRATIONS_DIR.glob("*.sql"))[-1].name)

    out = runner.invoke(app, ["engine", "performance", "--json"])
    assert out.exit_code == 1
    doc = json.loads(out.output)
    assert doc["error"] == "SchemaBehindError"
    assert "behind this build" in doc["message"]
