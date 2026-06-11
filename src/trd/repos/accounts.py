import duckdb

from trd.models import Account, AccountType

_COLS = "id, name, type, currency"


def _row_to_account(row: tuple) -> Account:
    return Account(id=row[0], name=row[1], type=row[2], currency=row[3])


class AccountRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get_by_name(self, name: str) -> Account | None:
        row = self.conn.execute(f"SELECT {_COLS} FROM account WHERE name = ?", [name]).fetchone()
        return _row_to_account(row) if row else None

    def create(self, name: str, type_: AccountType, currency: str = "USD") -> Account:
        row = self.conn.execute(
            """
            INSERT INTO account (name, type, currency)
            VALUES (?, ?, ?)
            RETURNING id, name, type, currency
            """,
            [name, type_.value, currency],
        ).fetchone()
        assert row is not None
        return _row_to_account(row)

    def get_or_create(self, name: str, type_: AccountType) -> Account:
        return self.get_by_name(name) or self.create(name, type_)

    def list_all(self) -> list[Account]:
        rows = self.conn.execute(f"SELECT {_COLS} FROM account ORDER BY name").fetchall()
        return [_row_to_account(r) for r in rows]
