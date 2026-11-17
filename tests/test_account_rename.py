"""Renaming an account (SB-1420).

DuckDB refuses an UPDATE of any row another table references, so a rename is an
override row read through the `account_named` view. What is worth pinning: an
account that holds things can be renamed, everything follows it, the old name
stops resolving and cannot be reused, and backups carry the new name.
"""

import duckdb
import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from trd.cli.app import app
from trd.errors import AccountNameTakenError, UnknownAccountError
from trd.models import AccountType
from trd.repos import AccountRepo
from trd.services.backup import export_data

runner = CliRunner()


def _with_a_trade(conn: duckdb.DuckDBPyConnection) -> int:
    account = AccountRepo(conn).create("old-ira", AccountType.REAL)
    instrument = conn.execute(
        "INSERT INTO instrument (symbol, type) VALUES ('AAPL', 'stock') RETURNING id"
    ).fetchone()
    assert instrument is not None
    conn.execute(
        """INSERT INTO txn (account_id, instrument_id, side, quantity, price, fees, executed_at)
           VALUES (?, ?, 'buy', 1, 100, 0, now())""",
        [account.id, instrument[0]],
    )
    return account.id


def test_a_referenced_account_renames_and_keeps_its_rows(conn: duckdb.DuckDBPyConnection) -> None:
    account_id = _with_a_trade(conn)
    repo = AccountRepo(conn)
    renamed = repo.rename("old-ira", "Self IRA")
    assert renamed.id == account_id and renamed.name == "Self IRA"
    assert repo.get_by_name("old-ira") is None
    assert [a.name for a in repo.list_all()] == ["Self IRA"]
    count = conn.execute("SELECT count(*) FROM txn WHERE account_id = ?", [account_id]).fetchone()
    assert count == (1,)


def test_backup_carries_the_new_name(conn: duckdb.DuckDBPyConnection) -> None:
    _with_a_trade(conn)
    AccountRepo(conn).rename("old-ira", "Self IRA")
    data = export_data(conn)
    assert [a["name"] for a in data["accounts"]] == ["Self IRA"]
    assert {t["account"] for t in data["transactions"]} == {"Self IRA"}


def test_taken_names_are_refused(conn: duckdb.DuckDBPyConnection) -> None:
    repo = AccountRepo(conn)
    repo.create("a", AccountType.REAL)
    repo.create("b", AccountType.REAL)
    with pytest.raises(AccountNameTakenError):
        repo.rename("a", "b")
    repo.rename("a", "c")
    # 'a' is still the row's original name: handing it out again would collide.
    with pytest.raises(AccountNameTakenError):
        repo.create("a", AccountType.REAL)
    with pytest.raises(AccountNameTakenError):
        repo.rename("b", "a")
    with pytest.raises(UnknownAccountError):
        repo.rename("a", "d")


def test_rename_again_and_back(conn: duckdb.DuckDBPyConnection) -> None:
    repo = AccountRepo(conn)
    repo.create("a", AccountType.REAL)
    repo.rename("a", "b")
    repo.rename("b", "c")
    assert [x.name for x in repo.list_all()] == ["c"]
    repo.rename("c", "a")
    assert [x.name for x in repo.list_all()] == ["a"]
    assert conn.execute("SELECT count(*) FROM account_rename").fetchone() == (0,)


def test_cli_rename(cli_env: FakeProvider) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["account", "add", "ira"]).exit_code == 0
    result = runner.invoke(app, ["account", "rename", "ira", "Self IRA"])
    assert result.exit_code == 0, result.output
    assert "Self IRA" in runner.invoke(app, ["account", "ls"]).output
    result = runner.invoke(app, ["buy", "AAPL", "1", "--price", "100", "--account", "Self IRA"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["account", "rename", "nope", "x"])
    assert result.exit_code == 1
