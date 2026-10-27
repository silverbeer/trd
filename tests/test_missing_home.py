"""An explicit TRD_HOME with no database is refused, never created (SB-1366).

A typo or an unmounted share used to get a fresh migrated trd.duckdb, which
reads as "new install" rather than "wrong path", and the error that followed
suggested `trd engine init`, the command that rewrites an engine's config.
"""

import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.config import missing_explicit_home
from trd.db.connection import connect
from trd.errors import MissingHomeError

runner = CliRunner()


@pytest.fixture
def missing(tmp_path, monkeypatch):
    home = tmp_path / "typo"
    monkeypatch.setenv("TRD_HOME", str(home))
    return home


@pytest.mark.parametrize(
    "args", [["engine", "report"], ["engine", "init"], ["portfolio"], ["account", "ls"]]
)
def test_refuses_and_creates_nothing(missing, args) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert isinstance(result.exception, MissingHomeError)
    assert str(missing) in str(result.exception)
    assert not missing.exists()


def test_an_existing_directory_without_a_database_is_refused(missing) -> None:
    missing.mkdir()
    result = runner.invoke(app, ["portfolio"])
    assert isinstance(result.exception, MissingHomeError)
    assert list(missing.iterdir()) == []


def test_init_creates_the_home(missing) -> None:
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    assert (missing / "trd.duckdb").is_file()
    assert runner.invoke(app, ["account", "ls"]).exit_code == 0


def test_restore_may_create_the_home(missing, tmp_path, monkeypatch) -> None:
    src = tmp_path / "src"
    connect(src / "trd.duckdb").close()
    backup = tmp_path / "backup.json"
    monkeypatch.setenv("TRD_HOME", str(src))
    assert runner.invoke(app, ["backup", str(backup)]).exit_code == 0
    monkeypatch.setenv("TRD_HOME", str(missing))
    result = runner.invoke(app, ["restore", str(backup)])
    assert result.exit_code == 0, result.output
    assert (missing / "trd.duckdb").is_file()


def test_missing_explicit_home(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("TRD_HOME", raising=False)
    assert missing_explicit_home() is None  # the default ~/.trd is never refused
    monkeypatch.setenv("TRD_HOME", str(tmp_path))
    assert missing_explicit_home() == tmp_path
    connect(tmp_path / "trd.duckdb").close()
    assert missing_explicit_home() is None
