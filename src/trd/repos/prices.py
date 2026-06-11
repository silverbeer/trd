from datetime import datetime
from decimal import Decimal

import duckdb

from trd.models import DailyBar


class PriceRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def upsert_daily(self, instrument_id: int, bars: list[DailyBar]) -> int:
        for bar in bars:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO price_daily
                    (instrument_id, date, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [instrument_id, bar.date, bar.open, bar.high, bar.low, bar.close, bar.volume],
            )
        return len(bars)

    def insert_snapshot(
        self, instrument_id: int, price: Decimal, prev_close: Decimal | None
    ) -> None:
        self.conn.execute(
            "INSERT INTO quote_snapshot (instrument_id, price, prev_close) VALUES (?, ?, ?)",
            [instrument_id, price, prev_close],
        )

    def latest_snapshot(
        self, instrument_id: int
    ) -> tuple[Decimal, Decimal | None, datetime] | None:
        """Most recent captured quote: (price, prev_close, captured_at)."""
        row = self.conn.execute(
            """
            SELECT price, prev_close, captured_at FROM quote_snapshot
            WHERE instrument_id = ? ORDER BY captured_at DESC LIMIT 1
            """,
            [instrument_id],
        ).fetchone()
        return (row[0], row[1], row[2]) if row else None
