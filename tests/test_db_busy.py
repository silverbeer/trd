from pathlib import Path

import duckdb
import pytest

import trd.db.connection as connection
from trd.errors import DatabaseBusyError, TrdError


def test_lock_io_error_becomes_database_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_connect(_path: str):
        raise duckdb.IOException("Could not set lock on file: Conflicting lock is held")

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    with pytest.raises(DatabaseBusyError, match="busy"):
        connection.connect(tmp_path / "x.duckdb")


def test_non_lock_io_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_connect(_path: str):
        raise duckdb.IOException("disk full")

    monkeypatch.setattr(duckdb, "connect", fake_connect)
    with pytest.raises(duckdb.IOException, match="disk full"):
        connection.connect(tmp_path / "x.duckdb")


def test_database_busy_is_trderror() -> None:
    assert isinstance(DatabaseBusyError(), TrdError)


def test_main_renders_trderror_cleanly(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import trd.cli.app as cli

    def boom() -> None:
        raise DatabaseBusyError()

    monkeypatch.setattr(cli, "app", boom)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    err = capsys.readouterr().err
    assert "busy" in err
    assert "Traceback" not in err
