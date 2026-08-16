from datetime import date, timedelta

import duckdb

from trd.models import EarningsDate, EarningsEvent, EarningsResult
from trd.repos.instruments import _row_to_instrument

# Column order, written once so the SELECT, the INSERT and the row mapper cannot
# drift apart — this table has fifteen columns, most of them nullable, and a
# silent off-by-one between them would land real numbers in the wrong fields.
_RESULT_COLUMNS = (
    "instrument_id,released_on,release_timing,source,source_observed_at,eps_actual,"
    "eps_estimate_pre_release,revenue_actual,revenue_estimate_pre_release,"
    "guidance_direction,next_quarter_revision_pct,next_year_revision_pct,"
    "earnings_day_return_pct,earnings_day_relative_strength_pct,quality_status"
)

# Which observation flag a filled field earns. `quality_status` is the record of
# what was genuinely measured, so a NULL can never be read as a measurement of
# zero — the failure mode the feature request calls out by name.
_FLAGS = {
    "eps_actual": "eps",
    "eps_estimate_pre_release": "estimate",
    "revenue_actual": "revenue",
    "guidance_direction": "guidance",
    "earnings_day_return_pct": "reaction",
    "earnings_day_relative_strength_pct": "relative_strength",
}


def _row_to_result(row: tuple) -> EarningsResult:
    return EarningsResult(
        instrument_id=row[0],
        released_on=row[1],
        release_timing=row[2],
        source=row[3],
        source_observed_at=row[4],
        eps_actual=row[5],
        eps_estimate_pre_release=row[6],
        revenue_actual=row[7],
        revenue_estimate_pre_release=row[8],
        guidance_direction=row[9],
        next_quarter_revision_pct=row[10],
        next_year_revision_pct=row[11],
        earnings_day_return_pct=row[12],
        earnings_day_relative_strength_pct=row[13],
        quality_status=row[14],
    )


class EarningsRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def upsert(
        self, instrument_id: int, events: list[EarningsDate], today: date | None = None
    ) -> int:
        """Store the provider's events, and drop future dates it no longer reports.

        The reconcile is the point. The primary key is (instrument_id, date), so
        a rescheduled report used to *add* a row rather than replace one: a
        company moving from the 20th to the 27th left both dates on record, and
        the entry blackout duly sat out two windows instead of one, with nothing
        in the UI to explain the second.

        Only future dates are reconciled. A past event is a fact — it has an
        `eps_actual` hanging off it and a result row referencing it — and the
        provider dropping it from a 12-quarter window is a horizon, not a
        cancellation.

        A provider that returns nothing reconciles nothing. Deleting every known
        date on an empty response would quietly remove the protection the
        blackout exists to give, and 'no response' and 'no earnings scheduled'
        are indistinguishable here.
        """
        for event in events:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO earnings_event
                    (instrument_id, date, eps_estimate, eps_actual)
                VALUES (?, ?, ?, ?)
                """,
                [instrument_id, event.date, event.eps_estimate, event.eps_actual],
            )
        if events:
            self.reconcile_future(instrument_id, [e.date for e in events], today)
        return len(events)

    def reconcile_future(
        self, instrument_id: int, keep: list[date], today: date | None = None
    ) -> int:
        """Delete future earnings dates absent from `keep`. Returns rows removed."""
        today = today or date.today()
        placeholders = ",".join("?" for _ in keep) or "NULL"
        return len(
            self.conn.execute(
                f"""
                DELETE FROM earnings_event
                WHERE instrument_id = ? AND date > ? AND date NOT IN ({placeholders})
                RETURNING date
                """,
                [instrument_id, today, *keep],
            ).fetchall()
        )

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

    def dates_for_instrument(self, instrument_id: int) -> list[date]:
        """Every known earnings date for one instrument, oldest first. The backtest
        loads these once and checks its blackout window in memory — one query per
        symbol instead of one per simulated day."""
        rows = self.conn.execute(
            "SELECT date FROM earnings_event WHERE instrument_id = ? ORDER BY date",
            [instrument_id],
        ).fetchall()
        return [r[0] for r in rows]

    def next_for_instrument(self, instrument_id: int, start: date | None = None) -> date | None:
        start = start or date.today()
        row = self.conn.execute(
            "SELECT min(date) FROM earnings_event WHERE instrument_id = ? AND date >= ?",
            [instrument_id, start],
        ).fetchone()
        return row[0] if row else None


class EarningsResultRepo:
    """The point-in-time archive. Write-once by design.

    Every method here exists to protect one invariant: a row records what was
    known when it was written, so nothing may overwrite an observation with a
    later one. The only permitted writes are the first insert and filling a
    field that was genuinely unknown at insert time — an `eps_actual` that
    arrives after the release, or a reaction that could not be computed until
    the session settled.
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get(self, instrument_id: int, released_on: date) -> EarningsResult | None:
        row = self.conn.execute(
            f"SELECT {_RESULT_COLUMNS} FROM earnings_result "
            "WHERE instrument_id = ? AND released_on = ?",
            [instrument_id, released_on],
        ).fetchone()
        return _row_to_result(row) if row else None

    def record(self, result: EarningsResult) -> bool:
        """Insert a result if this release is not already on record.

        Returns True when a row was written. An existing row is left exactly as
        it was — including its estimate, which is the field a later sync would
        otherwise silently revise into a number nobody traded against.
        """
        if self.get(result.instrument_id, result.released_on) is not None:
            return False
        self.conn.execute(
            f"INSERT INTO earnings_result ({_RESULT_COLUMNS}) "
            f"VALUES ({','.join('?' for _ in _RESULT_COLUMNS.split(','))})",
            [
                result.instrument_id,
                result.released_on,
                result.release_timing,
                result.source,
                result.source_observed_at,
                result.eps_actual,
                result.eps_estimate_pre_release,
                result.revenue_actual,
                result.revenue_estimate_pre_release,
                result.guidance_direction,
                result.next_quarter_revision_pct,
                result.next_year_revision_pct,
                result.earnings_day_return_pct,
                result.earnings_day_relative_strength_pct,
                result.quality_status,
            ],
        )
        return True

    def fill_unknown(self, instrument_id: int, released_on: date, **fields: object) -> bool:
        """Fill fields that are still NULL on a stored row. Never overwrites.

        For values that could not exist at insert time — the reported EPS on a
        row first seen before the release, or the reaction before the session
        closed. A field already carrying a value is left alone, so this can run
        on every sync without eroding the archive.
        """
        stored = self.get(instrument_id, released_on)
        if stored is None:
            return False
        pending = {
            key: value
            for key, value in fields.items()
            if value is not None and getattr(stored, key, None) is None
        }
        if not pending:
            return False
        observed = set(stored.observed) | {_FLAGS[key] for key in pending if key in _FLAGS}
        assignments = ", ".join(f"{key} = ?" for key in pending)
        self.conn.execute(
            f"UPDATE earnings_result SET {assignments}, quality_status = ? "
            "WHERE instrument_id = ? AND released_on = ?",
            [*pending.values(), ",".join(sorted(observed)), instrument_id, released_on],
        )
        return True

    def for_instrument(self, instrument_id: int, limit: int | None = None) -> list[EarningsResult]:
        """Stored results, newest first."""
        sql = (
            f"SELECT {_RESULT_COLUMNS} FROM earnings_result "
            "WHERE instrument_id = ? ORDER BY released_on DESC"
        )
        params: list[object] = [instrument_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_result(row) for row in self.conn.execute(sql, params).fetchall()]

    def reconcile_future(
        self, instrument_id: int, keep: list[date], today: date | None = None
    ) -> int:
        """Drop future rows for releases the provider no longer schedules.

        Estimates are captured when a release is first *announced*, not after it
        reports — capturing afterwards would store the post-release number, which
        is the corruption this table exists to prevent. The cost of recording
        early is that a rescheduled report leaves a row describing a release that
        never happened on that date.

        `eps_actual IS NULL` is the safety catch, and it is not a detail: a row
        that ever reported is history and must survive any reconcile. Only a
        future row that never became a result can be removed.
        """
        today = today or date.today()
        placeholders = ",".join("?" for _ in keep) or "NULL"
        return len(
            self.conn.execute(
                f"""
                DELETE FROM earnings_result
                WHERE instrument_id = ? AND released_on > ? AND eps_actual IS NULL
                  AND released_on NOT IN ({placeholders})
                RETURNING released_on
                """,
                [instrument_id, today, *keep],
            ).fetchall()
        )

    def count(self) -> int:
        row = self.conn.execute("SELECT count(*) FROM earnings_result").fetchone()
        return int(row[0]) if row else 0
