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


def test_connect_retries_while_the_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held lock is nearly always another process's short write — the engine
    CronJob scans every five minutes — so waiting beats erroring."""
    monkeypatch.setenv("TRD_DB_BACKOFF", "0,0,0")
    slept: list[float] = []
    monkeypatch.setattr(connection.time, "sleep", slept.append)

    real = duckdb.connect
    attempts = {"n": 0}

    def flaky(path: str):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise duckdb.IOException("Could not set lock on file: Conflicting lock is held")
        return real(path)

    monkeypatch.setattr(duckdb, "connect", flaky)
    conn = connection.connect(tmp_path / "x.duckdb")
    assert attempts["n"] == 3  # failed twice, third succeeded
    assert len(slept) == 2
    conn.close()


def test_connect_gives_up_after_the_backoff_is_exhausted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounded on purpose: a genuinely stuck writer must still surface."""
    monkeypatch.setenv("TRD_DB_BACKOFF", "0,0")
    monkeypatch.setattr(connection.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        duckdb,
        "connect",
        lambda _p: (_ for _ in ()).throw(duckdb.IOException("Conflicting lock is held")),
    )
    with pytest.raises(DatabaseBusyError):
        connection.connect(tmp_path / "x.duckdb")


def test_a_non_lock_error_is_never_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRD_DB_BACKOFF", "0,0,0")
    calls = {"n": 0}

    def boom(_path: str):
        calls["n"] += 1
        raise duckdb.IOException("disk full")

    monkeypatch.setattr(duckdb, "connect", boom)
    with pytest.raises(duckdb.IOException, match="disk full"):
        connection.connect(tmp_path / "x.duckdb")
    assert calls["n"] == 1  # retrying a full disk helps nobody


def test_wait_seconds_extends_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """SB-1507: the report jobs outwait a close sync holding the writer for minutes."""
    monkeypatch.delenv("TRD_DB_BACKOFF", raising=False)
    monkeypatch.delenv("TRD_DB_WAIT_SECONDS", raising=False)
    assert connection._backoff() == connection._BACKOFF_SECONDS
    monkeypatch.setenv("TRD_DB_WAIT_SECONDS", "600")
    waits = connection._backoff()
    assert waits[: len(connection._BACKOFF_SECONDS)] == connection._BACKOFF_SECONDS
    assert sum(waits) - sum(connection._BACKOFF_SECONDS) == pytest.approx(600)
    assert max(waits) == connection._WAIT_POLL_SECONDS
    monkeypatch.setenv("TRD_DB_WAIT_SECONDS", "soon")
    assert connection._backoff() == connection._BACKOFF_SECONDS


def test_read_only_open_waits_out_a_long_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 16:20 live report failed after ~8s while the close sync held the lock
    for five minutes. With a wait set, the read-only open rides it out."""
    db = tmp_path / "x.duckdb"
    connection.connect(db).close()
    monkeypatch.setenv("TRD_DB_BACKOFF", "0")
    monkeypatch.setenv("TRD_DB_WAIT_SECONDS", "300")
    slept: list[float] = []
    monkeypatch.setattr(connection.time, "sleep", slept.append)
    real = duckdb.connect
    attempts = {"n": 0}

    def busy_for_a_while(path: str, read_only: bool = False):
        attempts["n"] += 1
        if attempts["n"] <= 20:
            raise duckdb.IOException("Could not set lock on file: Conflicting lock is held")
        return real(path, read_only=read_only)

    monkeypatch.setattr(duckdb, "connect", busy_for_a_while)
    conn = connection.connect_read_only(db)
    conn.close()
    assert attempts["n"] == 21
    assert sum(slept) == pytest.approx(190)  # 0 + 19 polls of 10s
