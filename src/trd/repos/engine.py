import json
from datetime import date, datetime
from decimal import Decimal

import duckdb

from trd.models import (
    EngineConfig,
    EnginePosition,
    EngineRun,
    EngineSignal,
    Instrument,
    PositionStatus,
    SizingMode,
)
from trd.repos.instruments import _row_to_instrument

# Kept here rather than imported from the service: repos must not depend on services.
DEFAULT_EARNINGS_BLACKOUT_DAYS = 3

_CONFIG_COLS = (
    "id, account_id, watchlist, position_size, max_positions, strategies, exit_params, "
    "earnings_blackout_days, sizing_mode"
)
_SIGNAL_COLS = (
    "id, run_id, instrument_id, strategy, bar_date, fired_at, price, score, reason, acted"
)
_POSITION_COLS = (
    "id, account_id, instrument_id, signal_id, strategy, opened_at, entry_price, quantity, "
    "stop_price, target_price, atr_at_entry, trail_high, bars_held, last_bar_date, status, "
    "closed_at, exit_price, exit_reason"
)
_RUN_COLS = "id, started_at, scanned, signals, opened, closed, paper, note"


def _row_to_config(row: tuple) -> EngineConfig:
    return EngineConfig(
        id=row[0],
        account_id=row[1],
        watchlist=row[2],
        position_size=row[3],
        max_positions=row[4],
        strategies=json.loads(row[5]),
        exit_params=json.loads(row[6]),
        # Nullable in the schema: the column is added bare because DuckDB rejects
        # ADD COLUMN with a constraint, so a pre-migration row can still read NULL.
        earnings_blackout_days=row[7] if row[7] is not None else DEFAULT_EARNINGS_BLACKOUT_DAYS,
        # Nullable for the same reason: a row written before migration 013 reads
        # NULL and must keep the behaviour it was created with.
        sizing_mode=SizingMode(row[8]) if row[8] is not None else SizingMode.EXPOSURE,
    )


def _row_to_signal(row: tuple) -> EngineSignal:
    return EngineSignal(
        id=row[0],
        run_id=row[1],
        instrument_id=row[2],
        strategy=row[3],
        bar_date=row[4],
        fired_at=row[5],
        price=row[6],
        score=row[7],
        reason=row[8],
        acted=row[9],
    )


def _row_to_position(row: tuple) -> EnginePosition:
    return EnginePosition(
        id=row[0],
        account_id=row[1],
        instrument_id=row[2],
        signal_id=row[3],
        strategy=row[4],
        opened_at=row[5],
        entry_price=row[6],
        quantity=row[7],
        stop_price=row[8],
        target_price=row[9],
        atr_at_entry=row[10],
        trail_high=row[11],
        bars_held=row[12],
        last_bar_date=row[13],
        status=row[14],
        closed_at=row[15],
        exit_price=row[16],
        exit_reason=row[17],
    )


def _row_to_run(row: tuple) -> EngineRun:
    return EngineRun(
        id=row[0],
        started_at=row[1],
        scanned=row[2],
        signals=row[3],
        opened=row[4],
        closed=row[5],
        paper=row[6],
        note=row[7],
    )


class EngineConfigRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get(self) -> EngineConfig | None:
        """The engine runs one config at a time — the most recently created wins."""
        row = self.conn.execute(
            f"SELECT {_CONFIG_COLS} FROM engine_config ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return _row_to_config(row) if row else None

    def upsert(
        self,
        account_id: int,
        watchlist: str,
        position_size: Decimal,
        max_positions: int,
        strategies: list[str],
        exit_params: dict[str, float],
        earnings_blackout_days: int = DEFAULT_EARNINGS_BLACKOUT_DAYS,
        sizing_mode: SizingMode = SizingMode.EXPOSURE,
    ) -> EngineConfig:
        self.conn.execute("DELETE FROM engine_config WHERE account_id = ?", [account_id])
        row = self.conn.execute(
            f"""
            INSERT INTO engine_config
                (account_id, watchlist, position_size, max_positions, strategies, exit_params,
                 earnings_blackout_days, sizing_mode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_CONFIG_COLS}
            """,
            [
                account_id,
                watchlist,
                position_size,
                max_positions,
                json.dumps(strategies),
                json.dumps(exit_params),
                earnings_blackout_days,
                sizing_mode.value,
            ],
        ).fetchone()
        assert row is not None
        return _row_to_config(row)


class EngineRunRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def start(self, started_at: datetime, paper: bool) -> EngineRun:
        row = self.conn.execute(
            f"INSERT INTO engine_run (started_at, paper) VALUES (?, ?) RETURNING {_RUN_COLS}",
            [started_at, paper],
        ).fetchone()
        assert row is not None
        return _row_to_run(row)

    def finish(self, run_id: int, scanned: int, signals: int, opened: int, closed: int) -> None:
        self.conn.execute(
            """
            UPDATE engine_run SET scanned = ?, signals = ?, opened = ?, closed = ?
            WHERE id = ?
            """,
            [scanned, signals, opened, closed, run_id],
        )

    def list_recent(self, limit: int = 20) -> list[EngineRun]:
        rows = self.conn.execute(
            f"SELECT {_RUN_COLS} FROM engine_run ORDER BY started_at DESC, id DESC LIMIT ?",
            [limit],
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    def list_since(self, when: datetime, limit: int = 500) -> list[EngineRun]:
        """Scans since a moment, newest first."""
        rows = self.conn.execute(
            f"""
            SELECT {_RUN_COLS} FROM engine_run WHERE started_at >= ?
            ORDER BY started_at DESC, id DESC LIMIT ?
            """,
            [when, limit],
        ).fetchall()
        return [_row_to_run(r) for r in rows]

    def count_since(self, when: datetime) -> int:
        """Scans since a moment — 'has it run today' in one number. A silent engine
        and a broken one look identical until you count."""
        row = self.conn.execute(
            "SELECT count(*) FROM engine_run WHERE started_at >= ?", [when]
        ).fetchone()
        return row[0] if row else 0


class EngineSignalRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get(self, instrument_id: int, strategy: str, bar_date: date) -> EngineSignal | None:
        """The signal this rule already fired on this bar, if any. A 60-second loop
        re-derives the same signal all day; it is stored once and reconsidered as a
        candidate until it is acted on."""
        row = self.conn.execute(
            f"""
            SELECT {_SIGNAL_COLS} FROM engine_signal
            WHERE instrument_id = ? AND strategy = ? AND bar_date = ?
            """,
            [instrument_id, strategy, bar_date],
        ).fetchone()
        return _row_to_signal(row) if row else None

    def insert(
        self,
        run_id: int | None,
        instrument_id: int,
        strategy: str,
        bar_date: date,
        fired_at: datetime,
        price: Decimal,
        score: float,
        reason: str,
    ) -> EngineSignal:
        row = self.conn.execute(
            f"""
            INSERT INTO engine_signal
                (run_id, instrument_id, strategy, bar_date, fired_at, price, score, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_SIGNAL_COLS}
            """,
            [run_id, instrument_id, strategy, bar_date, fired_at, price, score, reason],
        ).fetchone()
        assert row is not None
        return _row_to_signal(row)

    def by_id(self, signal_id: int) -> EngineSignal | None:
        """The signal a position was opened from — the recorded 'why'."""
        row = self.conn.execute(
            f"SELECT {_SIGNAL_COLS} FROM engine_signal WHERE id = ?", [signal_id]
        ).fetchone()
        return _row_to_signal(row) if row else None

    def mark_acted(self, signal_id: int) -> None:
        self.conn.execute("UPDATE engine_signal SET acted = TRUE WHERE id = ?", [signal_id])

    def list_recent(
        self, limit: int = 50, strategy: str | None = None
    ) -> list[tuple[EngineSignal, Instrument]]:
        where = "WHERE s.strategy = ?" if strategy else ""
        params: list[object] = [strategy] if strategy else []
        params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT {", ".join(f"s.{c}" for c in _SIGNAL_COLS.split(", "))},
                   i.id, i.symbol, i.name, i.type, i.exchange, i.sector, i.currency
            FROM engine_signal s
            JOIN instrument i ON i.id = s.instrument_id
            {where}
            ORDER BY s.fired_at DESC, s.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [(_row_to_signal(r[:10]), _row_to_instrument(r[10:])) for r in rows]


class EnginePositionRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def open(
        self,
        account_id: int,
        instrument_id: int,
        signal_id: int | None,
        strategy: str,
        opened_at: datetime,
        entry_price: Decimal,
        quantity: Decimal,
        stop_price: Decimal,
        target_price: Decimal,
        atr_at_entry: Decimal,
        last_bar_date: date | None,
    ) -> EnginePosition:
        row = self.conn.execute(
            f"""
            INSERT INTO engine_position
                (account_id, instrument_id, signal_id, strategy, opened_at, entry_price,
                 quantity, stop_price, target_price, atr_at_entry, trail_high, last_bar_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_POSITION_COLS}
            """,
            [
                account_id,
                instrument_id,
                signal_id,
                strategy,
                opened_at,
                entry_price,
                quantity,
                stop_price,
                target_price,
                atr_at_entry,
                entry_price,  # trail_high starts at entry
                last_bar_date,
            ],
        ).fetchone()
        assert row is not None
        return _row_to_position(row)

    def _list(self, where: str, params: list[object]) -> list[tuple[EnginePosition, Instrument]]:
        rows = self.conn.execute(
            f"""
            SELECT {", ".join(f"p.{c}" for c in _POSITION_COLS.split(", "))},
                   i.id, i.symbol, i.name, i.type, i.exchange, i.sector, i.currency
            FROM engine_position p
            JOIN instrument i ON i.id = p.instrument_id
            {where}
            ORDER BY p.opened_at DESC, p.id DESC
            """,
            params,
        ).fetchall()
        return [(_row_to_position(r[:18]), _row_to_instrument(r[18:])) for r in rows]

    def list_open(self, account_id: int) -> list[tuple[EnginePosition, Instrument]]:
        return self._list("WHERE p.account_id = ? AND p.status = 'open'", [account_id])

    def list_closed(self, account_id: int) -> list[tuple[EnginePosition, Instrument]]:
        return self._list("WHERE p.account_id = ? AND p.status = 'closed'", [account_id])

    def list_all(self, account_id: int) -> list[tuple[EnginePosition, Instrument]]:
        return self._list("WHERE p.account_id = ?", [account_id])

    def has_open(self, account_id: int, instrument_id: int) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM engine_position
            WHERE account_id = ? AND instrument_id = ? AND status = 'open'
            """,
            [account_id, instrument_id],
        ).fetchone()
        return row is not None

    def touch(
        self, position_id: int, trail_high: Decimal, bars_held: int, last_bar_date: date | None
    ) -> None:
        """Advance the exit state machine: new high-water mark and bar count."""
        self.conn.execute(
            """
            UPDATE engine_position
            SET trail_high = ?, bars_held = ?, last_bar_date = ?
            WHERE id = ?
            """,
            [trail_high, bars_held, last_bar_date, position_id],
        )

    def close(
        self, position_id: int, closed_at: datetime, exit_price: Decimal, exit_reason: str
    ) -> None:
        self.conn.execute(
            """
            UPDATE engine_position
            SET status = ?, closed_at = ?, exit_price = ?, exit_reason = ?
            WHERE id = ?
            """,
            [PositionStatus.CLOSED.value, closed_at, exit_price, exit_reason, position_id],
        )
