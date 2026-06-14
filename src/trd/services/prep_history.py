"""Persist and query Sunday Prep briefings.

SundayPrepService stays pure (it never touches the DB). This is the thin layer that
flattens a briefing into queryable columns and stores it, so the week-ahead market
environment becomes a time series you can trend.
"""

import duckdb

from trd.repos import PrepSnapshotRepo, PrepSnapshotRow
from trd.services.sunday_prep import SundayPrepBriefing


def _briefing_to_row(b: SundayPrepBriefing) -> PrepSnapshotRow:
    pcts = [float(f.change_pct) for f in b.futures if f.change_pct is not None]
    avg = round(sum(pcts) / len(pcts), 4) if pcts else None
    top = b.sector_leaders[0] if b.sector_leaders else None
    worst = b.sector_laggards[0] if b.sector_laggards else None
    return PrepSnapshotRow(
        snapshot_date=b.generated_for,
        week_start=b.week_start,
        week_end=b.week_end,
        vix=float(b.volatility.vix) if b.volatility.vix is not None else None,
        vix_band=b.volatility.band,
        avg_futures_pct=avg,
        top_sector=top.symbol if top else None,
        top_sector_pct=float(top.week_pct) if top and top.week_pct is not None else None,
        worst_sector=worst.symbol if worst else None,
        worst_sector_pct=float(worst.week_pct) if worst and worst.week_pct is not None else None,
        fomc_week=any("FOMC" in e.name for e in b.econ_events),
        earnings_count=len(b.earnings),
    )


class PrepHistoryService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.repo = PrepSnapshotRepo(conn)

    def save(self, briefing: SundayPrepBriefing) -> None:
        self.repo.save(_briefing_to_row(briefing), briefing.model_dump_json())

    def history(self, limit: int = 26) -> list[PrepSnapshotRow]:
        return self.repo.history(limit)
