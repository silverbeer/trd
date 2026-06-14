from datetime import date, datetime, timedelta
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
                    (instrument_id, date, open, high, low, close, volume, adj_close)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    instrument_id,
                    bar.date,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.adj_close,
                ],
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

    def monthly_closes(self, instrument_id: int) -> list[tuple[date, Decimal]]:
        """Last close of each calendar month, dividend/split-adjusted when available.

        Return-series math must use adjusted closes or multi-year results silently
        drop dividends and break across splits.
        """
        rows = self.conn.execute(
            """
            SELECT CAST(date_trunc('month', date) AS DATE) AS month,
                   max_by(coalesce(adj_close, close), date) AS close
            FROM price_daily
            WHERE instrument_id = ?
            GROUP BY month
            ORDER BY month
            """,
            [instrument_id],
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def close_on_or_after(
        self, instrument_id: int, target: date, within_days: int = 7, adjusted: bool = False
    ) -> Decimal | None:
        """First daily close on/after target within the window (weekend/holiday-safe)."""
        column = "coalesce(adj_close, close)" if adjusted else "close"
        row = self.conn.execute(
            f"""
            SELECT {column} FROM price_daily
            WHERE instrument_id = ? AND date BETWEEN ? AND ?
            ORDER BY date LIMIT 1
            """,
            [instrument_id, target, target + timedelta(days=within_days)],
        ).fetchone()
        return row[0] if row else None

    def closes_in_range(
        self, instrument_id: int, start: date, end: date, adjusted: bool = True
    ) -> list[tuple[date, Decimal]]:
        """All (date, close) for an instrument in [start, end], oldest-first.
        Adjusted by default — return-series math must include dividends/splits."""
        column = "coalesce(adj_close, close)" if adjusted else "close"
        rows = self.conn.execute(
            f"""
            SELECT date, {column} FROM price_daily
            WHERE instrument_id = ? AND date BETWEEN ? AND ?
            ORDER BY date
            """,
            [instrument_id, start, end],
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def first_date(self, instrument_id: int) -> date | None:
        row = self.conn.execute(
            "SELECT min(date) FROM price_daily WHERE instrument_id = ?", [instrument_id]
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def recent_closes(self, instrument_id: int, days: int = 30) -> list[Decimal]:
        """The last N daily closes, oldest-first — for inline sparklines."""
        rows = self.conn.execute(
            "SELECT close FROM price_daily WHERE instrument_id = ? ORDER BY date DESC LIMIT ?",
            [instrument_id, days],
        ).fetchall()
        return [r[0] for r in reversed(rows)]

    def latest_close(self, instrument_id: int, adjusted: bool = False) -> Decimal | None:
        column = "coalesce(adj_close, close)" if adjusted else "close"
        row = self.conn.execute(
            f"SELECT {column} FROM price_daily WHERE instrument_id = ? ORDER BY date DESC LIMIT 1",
            [instrument_id],
        ).fetchone()
        return row[0] if row else None
