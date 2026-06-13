from pathlib import Path

import duckdb

from trd.errors import DatabaseBusyError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the database and bring schema up to date."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = duckdb.connect(str(db_path))
    except duckdb.IOException as exc:
        # Single-writer lock held by another trd process (common when the DB
        # lives on a synced drive). Surface a clean message, not a traceback.
        if "lock" in str(exc).lower():
            raise DatabaseBusyError() from exc
        raise
    apply_migrations(conn)
    return conn


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
