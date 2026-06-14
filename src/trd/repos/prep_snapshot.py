from datetime import date

import duckdb
from pydantic import BaseModel


class PrepSnapshotRow(BaseModel):
    """A persisted Sunday Prep briefing — the denormalized, queryable view.
    The full briefing stays in the JSON `payload` column."""

    snapshot_date: date
    week_start: date
    week_end: date
    vix: float | None = None
    vix_band: str = ""
    avg_futures_pct: float | None = None
    top_sector: str | None = None
    top_sector_pct: float | None = None
    worst_sector: str | None = None
    worst_sector_pct: float | None = None
    fomc_week: bool = False
    earnings_count: int = 0


# Columns selected for a PrepSnapshotRow, in order.
_ROW_COLS = (
    "snapshot_date, week_start, week_end, vix, vix_band, avg_futures_pct, "
    "top_sector, top_sector_pct, worst_sector, worst_sector_pct, fomc_week, earnings_count"
)


def _to_row(r: tuple) -> PrepSnapshotRow:
    return PrepSnapshotRow(
        snapshot_date=r[0],
        week_start=r[1],
        week_end=r[2],
        vix=float(r[3]) if r[3] is not None else None,
        vix_band=r[4] or "",
        avg_futures_pct=float(r[5]) if r[5] is not None else None,
        top_sector=r[6],
        top_sector_pct=float(r[7]) if r[7] is not None else None,
        worst_sector=r[8],
        worst_sector_pct=float(r[9]) if r[9] is not None else None,
        fomc_week=bool(r[10]),
        earnings_count=int(r[11]),
    )


class PrepSnapshotRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def save(self, row: PrepSnapshotRow, payload_json: str) -> None:
        """Upsert by snapshot_date — re-running a day overwrites that day's briefing."""
        self.conn.execute("DELETE FROM prep_snapshot WHERE snapshot_date = ?", [row.snapshot_date])
        self.conn.execute(
            """
            INSERT INTO prep_snapshot
                (snapshot_date, week_start, week_end, vix, vix_band, avg_futures_pct,
                 top_sector, top_sector_pct, worst_sector, worst_sector_pct,
                 fomc_week, earnings_count, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                row.snapshot_date,
                row.week_start,
                row.week_end,
                row.vix,
                row.vix_band,
                row.avg_futures_pct,
                row.top_sector,
                row.top_sector_pct,
                row.worst_sector,
                row.worst_sector_pct,
                row.fomc_week,
                row.earnings_count,
                payload_json,
            ],
        )

    def history(self, limit: int = 26) -> list[PrepSnapshotRow]:
        """Most recent snapshots first."""
        rows = self.conn.execute(
            f"SELECT {_ROW_COLS} FROM prep_snapshot ORDER BY snapshot_date DESC LIMIT ?",
            [limit],
        ).fetchall()
        return [_to_row(r) for r in rows]

    def latest_payload(self) -> str | None:
        row = self.conn.execute(
            "SELECT payload FROM prep_snapshot ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else None
