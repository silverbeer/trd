from pathlib import Path

from trd.db.connection import apply_migrations, connect

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
