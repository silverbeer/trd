from decimal import Decimal

import duckdb

from trd.models import ExitTrigger, Instrument
from trd.repos.instruments import _row_to_instrument

_COLS = "id, account_id, instrument_id, stop_price, target_price, note"


def _row_to_trigger(row: tuple) -> ExitTrigger:
    return ExitTrigger(
        id=row[0],
        account_id=row[1],
        instrument_id=row[2],
        stop_price=row[3],
        target_price=row[4],
        note=row[5],
    )


class ExitTriggerRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get(self, account_id: int, instrument_id: int) -> ExitTrigger | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM exit_trigger WHERE account_id = ? AND instrument_id = ?",
            [account_id, instrument_id],
        ).fetchone()
        return _row_to_trigger(row) if row else None

    def upsert(
        self,
        account_id: int,
        instrument_id: int,
        stop_price: Decimal | None,
        target_price: Decimal | None,
        note: str | None,
    ) -> ExitTrigger:
        """Create or replace the (account, instrument) trigger. One per holding."""
        existing = self.get(account_id, instrument_id)
        if existing is not None:
            row = self.conn.execute(
                f"""
                UPDATE exit_trigger SET stop_price = ?, target_price = ?, note = ?
                WHERE id = ? RETURNING {_COLS}
                """,
                [stop_price, target_price, note, existing.id],
            ).fetchone()
        else:
            row = self.conn.execute(
                f"""
                INSERT INTO exit_trigger (account_id, instrument_id, stop_price, target_price, note)
                VALUES (?, ?, ?, ?, ?) RETURNING {_COLS}
                """,
                [account_id, instrument_id, stop_price, target_price, note],
            ).fetchone()
        assert row is not None
        return _row_to_trigger(row)

    def remove(self, account_id: int, instrument_id: int) -> bool:
        """Delete the trigger. Returns False if there wasn't one."""
        if self.get(account_id, instrument_id) is None:
            return False
        self.conn.execute(
            "DELETE FROM exit_trigger WHERE account_id = ? AND instrument_id = ?",
            [account_id, instrument_id],
        )
        return True

    def list_all(self, account_id: int | None = None) -> list[tuple[ExitTrigger, Instrument, str]]:
        """(trigger, instrument, account_name) tuples, optionally scoped to one account."""
        sql = """
            SELECT e.id, e.account_id, e.instrument_id, e.stop_price, e.target_price, e.note,
                   i.id, i.symbol, i.name, i.type, i.exchange, i.sector, i.currency,
                   a.name
            FROM exit_trigger e
            JOIN instrument i ON i.id = e.instrument_id
            JOIN account a ON a.id = e.account_id
            {where}
            ORDER BY a.name, i.symbol
        """
        if account_id is None:
            rows = self.conn.execute(sql.format(where="")).fetchall()
        else:
            rows = self.conn.execute(
                sql.format(where="WHERE e.account_id = ?"), [account_id]
            ).fetchall()
        return [(_row_to_trigger(r[:6]), _row_to_instrument(r[6:13]), r[13]) for r in rows]
