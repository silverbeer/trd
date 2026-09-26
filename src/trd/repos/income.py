from datetime import datetime
from decimal import Decimal

import duckdb

from trd.models import Income, IncomeKind

_COLS = "id, account_id, instrument_id, kind, amount, received_at, note"


def _row_to_income(row: tuple) -> Income:
    return Income(
        id=row[0],
        account_id=row[1],
        instrument_id=row[2],
        kind=IncomeKind(row[3]),
        amount=row[4],
        received_at=row[5],
        note=row[6],
    )


class IncomeRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def add(
        self,
        account_id: int,
        amount: Decimal,
        received_at: datetime,
        kind: IncomeKind = IncomeKind.DIVIDEND,
        instrument_id: int | None = None,
        note: str | None = None,
    ) -> Income:
        row = self.conn.execute(
            """INSERT INTO income (account_id, instrument_id, kind, amount, received_at, note)
               VALUES (?, ?, ?, ?, ?, ?)
               RETURNING """
            + _COLS,
            [account_id, instrument_id, kind.value, amount, received_at, note],
        ).fetchone()
        assert row is not None
        return _row_to_income(row)

    def list_all(self, account_id: int | None = None) -> list[Income]:
        """Every payment, oldest first — the order a cash-flow series wants."""
        where, params = ("WHERE account_id = ?", [account_id]) if account_id else ("", [])
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM income {where} ORDER BY received_at, id", params
        ).fetchall()
        return [_row_to_income(r) for r in rows]

    def total(self, account_id: int | None = None) -> Decimal:
        where, params = ("WHERE account_id = ?", [account_id]) if account_id else ("", [])
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) FROM income {where}", params
        ).fetchone()
        return Decimal(str(row[0])) if row else Decimal(0)
