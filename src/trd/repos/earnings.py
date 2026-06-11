from datetime import date, timedelta

import duckdb

from trd.models import EarningsDate, EarningsEvent
from trd.repos.instruments import _row_to_instrument


class EarningsRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def upsert(self, instrument_id: int, events: list[EarningsDate]) -> int:
        for event in events:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO earnings_event
                    (instrument_id, date, eps_estimate, eps_actual)
                VALUES (?, ?, ?, ?)
                """,
                [instrument_id, event.date, event.eps_estimate, event.eps_actual],
            )
        return len(events)

    def upcoming(self, days: int, start: date | None = None) -> list[EarningsEvent]:
        """Earnings events in [start, start + days], soonest first."""
        start = start or date.today()
        rows = self.conn.execute(
            """
            SELECT e.date, e.eps_estimate, e.eps_actual,
                   i.id, i.symbol, i.name, i.type, i.exchange, i.sector, i.currency
            FROM earnings_event e
            JOIN instrument i ON i.id = e.instrument_id
            WHERE e.date BETWEEN ? AND ?
            ORDER BY e.date, i.symbol
            """,
            [start, start + timedelta(days=days)],
        ).fetchall()
        return [
            EarningsEvent(
                instrument=_row_to_instrument(r[3:]),
                date=r[0],
                eps_estimate=r[1],
                eps_actual=r[2],
            )
            for r in rows
        ]

    def next_for_instrument(self, instrument_id: int, start: date | None = None) -> date | None:
        start = start or date.today()
        row = self.conn.execute(
            "SELECT min(date) FROM earnings_event WHERE instrument_id = ? AND date >= ?",
            [instrument_id, start],
        ).fetchone()
        return row[0] if row else None
