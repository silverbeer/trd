"""Storage for dated decision reviews. One row per session per engine."""

import json
from datetime import date, datetime

import duckdb
from pydantic import BaseModel

_COLS = (
    "snapshot_date, generated_at, build, engine, trades_closed, signals_fired, "
    "findings, hypotheses, quiet"
)


class ReviewSnapshotRow(BaseModel):
    """A persisted review — the queryable view. The whole review stays in JSON."""

    snapshot_date: date
    generated_at: datetime
    build: str
    engine: str
    trades_closed: int = 0
    signals_fired: int = 0
    findings: int = 0
    hypotheses: int = 0
    quiet: bool = True


class ReviewSnapshotRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def save(self, row: ReviewSnapshotRow, payload: dict) -> None:
        """Replace the review for that session.

        Re-running a review for a past date is a legitimate thing to do — the
        arithmetic changes, or the follow-through window has since filled in —
        and it must leave one row, not two claims about the same day.
        """
        self.conn.execute(
            "DELETE FROM review_snapshot WHERE snapshot_date = ?", [row.snapshot_date]
        )
        self.conn.execute(
            f"INSERT INTO review_snapshot ({_COLS}, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                row.snapshot_date,
                row.generated_at,
                row.build,
                row.engine,
                row.trades_closed,
                row.signals_fired,
                row.findings,
                row.hypotheses,
                row.quiet,
                json.dumps(payload, default=str),
            ],
        )

    def list_recent(self, limit: int = 30) -> list[ReviewSnapshotRow]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM review_snapshot ORDER BY snapshot_date DESC LIMIT ?", [limit]
        ).fetchall()
        return [
            ReviewSnapshotRow(
                snapshot_date=r[0],
                generated_at=r[1],
                build=r[2],
                engine=r[3],
                trades_closed=r[4],
                signals_fired=r[5],
                findings=r[6],
                hypotheses=r[7],
                quiet=r[8],
            )
            for r in rows
        ]

    def recent_payloads(self, limit: int = 30) -> list[tuple[date, dict]]:
        """The stored reviews themselves, newest first.

        Findings live inside the payload rather than in columns of their own, so
        anything asking "has this finding persisted" reads them from here. One
        query rather than a date list followed by a fetch each: thirty snapshots
        of a dozen findings is nothing, and the round trips would be the cost.
        """
        rows = self.conn.execute(
            "SELECT snapshot_date, payload FROM review_snapshot "
            "ORDER BY snapshot_date DESC LIMIT ?",
            [limit],
        ).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def payload(self, on: date) -> dict | None:
        row = self.conn.execute(
            "SELECT payload FROM review_snapshot WHERE snapshot_date = ?", [on]
        ).fetchone()
        return json.loads(row[0]) if row else None
