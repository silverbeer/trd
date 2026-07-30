import os
import time
from pathlib import Path

import duckdb

from trd.errors import DatabaseBusyError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# A held lock is nearly always someone else's short write, not a stuck process:
# the engine CronJob scans every five minutes and holds the database only while
# it writes. Waiting briefly turns "the tool errored" into "the tool paused",
# which is the difference between the CLI being usable during market hours and
# not. Bounded, because a genuinely stuck writer should still surface.
_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0, 4.0)


def _backoff() -> tuple[float, ...]:
    """Override for tests, which must not actually sleep."""
    raw = os.environ.get("TRD_DB_BACKOFF")
    if raw is None:
        return _BACKOFF_SECONDS
    return tuple(float(part) for part in raw.split(",") if part.strip())


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the database and bring schema up to date.

    Retries while another process holds the single-writer lock. See _BACKOFF_SECONDS.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    waits = _backoff()
    for attempt in range(len(waits) + 1):
        try:
            conn = duckdb.connect(str(db_path))
        except duckdb.IOException as exc:
            # Single-writer lock held by another trd process (the engine CronJob,
            # or a DB on a synced drive). Surface a clean message, not a traceback.
            if "lock" not in str(exc).lower():
                raise
            if attempt == len(waits):
                raise DatabaseBusyError() from exc
            time.sleep(waits[attempt])
            continue
        apply_migrations(conn)
        return conn
    raise AssertionError("unreachable")  # pragma: no cover


def apply_migrations(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Apply numbered .sql migrations not yet recorded. Returns filenames applied."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations").fetchall()}
    ran: list[str] = []
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql_file.name in applied:
            continue
        conn.execute(sql_file.read_text())
        conn.execute("INSERT INTO schema_migrations (filename) VALUES (?)", [sql_file.name])
        ran.append(sql_file.name)
    return ran
