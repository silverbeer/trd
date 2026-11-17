from datetime import datetime

import duckdb

from trd.errors import AccountNameTakenError, TrdError, UnknownAccountError
from trd.models import Account, AccountType

_COLS = "id, name, type, currency"


def _row_to_account(row: tuple) -> Account:
    return Account(id=row[0], name=row[1], type=row[2], currency=row[3])


class AccountRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get_by_name(self, name: str) -> Account | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM account_named WHERE name = ?", [name]
        ).fetchone()
        return _row_to_account(row) if row else None

    def create(self, name: str, type_: AccountType, currency: str = "USD") -> Account:
        if self.name_taken(name):
            raise AccountNameTakenError(name)
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
        rows = self.conn.execute(f"SELECT {_COLS} FROM account_named ORDER BY name").fetchall()
        return [_row_to_account(r) for r in rows]

    def name_taken(self, name: str) -> bool:
        """A current name, or any account's ORIGINAL name: `account.name` keeps its
        unique constraint after a rename, so an old name cannot be handed out again."""
        row = self.conn.execute(
            """SELECT 1 FROM account_named WHERE name = ?
               UNION ALL SELECT 1 FROM account WHERE name = ?""",
            [name, name],
        ).fetchone()
        return row is not None

    def rename(self, old: str, new: str) -> Account:
        """Rename an account (SB-1420). Every table holds the account by id, so
        nothing else moves; see migration 036 for why it is not an UPDATE."""
        new = new.strip()
        if not new:
            raise TrdError("The new account name is empty.")
        account = self.get_by_name(old)
        if account is None:
            raise UnknownAccountError(old)
        if new == account.name:
            return account
        original = self.conn.execute(
            "SELECT name FROM account WHERE id = ?", [account.id]
        ).fetchone()
        assert original is not None
        if new == original[0]:  # back to where it started: drop the override
            self.conn.execute("DELETE FROM account_rename WHERE account_id = ?", [account.id])
        else:
            if self.name_taken(new):
                raise AccountNameTakenError(new)
            self.conn.execute(
                """INSERT INTO account_rename (account_id, name, renamed_at) VALUES (?, ?, ?)
                   ON CONFLICT (account_id) DO UPDATE
                   SET name = excluded.name, renamed_at = excluded.renamed_at""",
                [account.id, new, datetime.now()],
            )
        renamed = self.get_by_name(new)
        assert renamed is not None
        return renamed
