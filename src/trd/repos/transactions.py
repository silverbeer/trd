from datetime import datetime
from decimal import Decimal

import duckdb

from trd.models import Side, Transaction

_COLS = "id, account_id, instrument_id, side, quantity, price, fees, executed_at, note"


def _row_to_txn(row: tuple) -> Transaction:
    return Transaction(
        id=row[0],
        account_id=row[1],
        instrument_id=row[2],
        side=row[3],
        quantity=row[4],
        price=row[5],
        fees=row[6],
        executed_at=row[7],
        note=row[8],
    )


class TransactionRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def insert(
        self,
        account_id: int,
        instrument_id: int,
        side: Side,
        quantity: Decimal,
        price: Decimal,
        fees: Decimal,
        executed_at: datetime,
        note: str | None = None,
    ) -> Transaction:
        row = self.conn.execute(
            f"""
            INSERT INTO txn
                (account_id, instrument_id, side, quantity, price, fees, executed_at, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_COLS}
            """,
            [account_id, instrument_id, side.value, quantity, price, fees, executed_at, note],
        ).fetchone()
        assert row is not None
        return _row_to_txn(row)

    def list_chronological(self, account_id: int | None = None) -> list[Transaction]:
        """All transactions oldest-first (FIFO order), optionally scoped to one account."""
        if account_id is None:
            rows = self.conn.execute(f"SELECT {_COLS} FROM txn ORDER BY executed_at, id").fetchall()
        else:
            rows = self.conn.execute(
                f"SELECT {_COLS} FROM txn WHERE account_id = ? ORDER BY executed_at, id",
                [account_id],
            ).fetchall()
        return [_row_to_txn(r) for r in rows]
