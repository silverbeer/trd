"""Storage for what a trade did while it was on, and what a passed signal did next.

Two tables, two keys, one shape: computed once after the fact, read many times.
Nothing here computes — the arithmetic lives in `services/outcomes.py`, and this
only knows how to write it down and hand it back.
"""

from datetime import datetime
from decimal import Decimal

import duckdb

from trd.models import EnginePosition, EngineSignal, Instrument, SignalOutcome, TradeOutcome
from trd.repos.engine import _POSITION_COLS, _SIGNAL_COLS, _row_to_position, _row_to_signal
from trd.repos.instruments import _row_to_instrument

_TRADE_COLS = (
    "position_id, computed_at, timeframe, bars_seen, risk_per_share, "
    "mae_r, mae_at, mae_bar, mfe_r, mfe_at, mfe_bar, exit_r, capture, "
    "follow_through_r, follow_through_bars, follow_through_seen"
)
_SIGNAL_OUTCOME_COLS = (
    "signal_id, computed_at, timeframe, acted, capacity_blocked, open_positions, "
    "max_positions, risk_per_share, horizon_bars, bars_seen, mae_r, mfe_r, mfe_bar, "
    "forward_r, resolution"
)


def _row_to_trade_outcome(row: tuple) -> TradeOutcome:
    return TradeOutcome(
        position_id=row[0],
        computed_at=row[1],
        timeframe=row[2],
        bars_seen=row[3],
        risk_per_share=row[4],
        mae_r=row[5],
        mae_at=row[6],
        mae_bar=row[7],
        mfe_r=row[8],
        mfe_at=row[9],
        mfe_bar=row[10],
        exit_r=row[11],
        capture=row[12],
        follow_through_r=row[13],
        follow_through_bars=row[14],
        follow_through_seen=row[15],
    )


def _row_to_signal_outcome(row: tuple) -> SignalOutcome:
    return SignalOutcome(
        signal_id=row[0],
        computed_at=row[1],
        timeframe=row[2],
        acted=row[3],
        capacity_blocked=row[4],
        open_positions=row[5],
        max_positions=row[6],
        risk_per_share=row[7],
        horizon_bars=row[8],
        bars_seen=row[9],
        mae_r=row[10],
        mfe_r=row[11],
        mfe_bar=row[12],
        forward_r=row[13],
        resolution=row[14],
    )


class TradeOutcomeRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def upsert(self, outcome: TradeOutcome) -> None:
        """Replace by key, so a backfill can be run twice and mean the same thing.

        DELETE then INSERT rather than an UPDATE-else-INSERT dance: the row is
        derived data with one owner, and re-deriving it is the only way it ever
        changes.
        """
        self.conn.execute("DELETE FROM trade_outcome WHERE position_id = ?", [outcome.position_id])
        self.conn.execute(
            f"INSERT INTO trade_outcome ({_TRADE_COLS}) "
            f"VALUES ({', '.join('?' * len(_TRADE_COLS.split(', ')))})",
            [
                outcome.position_id,
                outcome.computed_at,
                outcome.timeframe,
                outcome.bars_seen,
                outcome.risk_per_share,
                outcome.mae_r,
                outcome.mae_at,
                outcome.mae_bar,
                outcome.mfe_r,
                outcome.mfe_at,
                outcome.mfe_bar,
                outcome.exit_r,
                outcome.capture,
                outcome.follow_through_r,
                outcome.follow_through_bars,
                outcome.follow_through_seen,
            ],
        )

    def measured_ids(self) -> set[int]:
        rows = self.conn.execute("SELECT position_id FROM trade_outcome").fetchall()
        return {int(r[0]) for r in rows}

    def list_all(self) -> list[TradeOutcome]:
        rows = self.conn.execute(
            f"SELECT {_TRADE_COLS} FROM trade_outcome ORDER BY position_id"
        ).fetchall()
        return [_row_to_trade_outcome(r) for r in rows]

    def joined(
        self, limit: int | None = None
    ) -> list[tuple[TradeOutcome, EnginePosition, Instrument]]:
        """Outcomes with the trade and the name they describe, newest first —
        what any human-facing view of this needs and what a JSON reader would
        otherwise have to stitch together itself."""
        n_out = len(_TRADE_COLS.split(", "))
        n_pos = len(_POSITION_COLS.split(", "))
        rows = self.conn.execute(
            f"""
            SELECT {", ".join(f"o.{c}" for c in _TRADE_COLS.split(", "))},
                   {", ".join(f"p.{c}" for c in _POSITION_COLS.split(", "))},
                   i.id, i.symbol, i.name, i.type, i.exchange, i.sector, i.currency, i.tradable
            FROM trade_outcome o
            JOIN engine_position p ON p.id = o.position_id
            JOIN instrument i ON i.id = p.instrument_id
            ORDER BY p.closed_at DESC, p.id DESC
            {"LIMIT ?" if limit else ""}
            """,
            [limit] if limit else [],
        ).fetchall()
        return [
            (
                _row_to_trade_outcome(r[:n_out]),
                _row_to_position(r[n_out : n_out + n_pos]),
                _row_to_instrument(r[n_out + n_pos :]),
            )
            for r in rows
        ]


class SignalOutcomeRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def upsert(self, outcome: SignalOutcome) -> None:
        self.conn.execute("DELETE FROM signal_outcome WHERE signal_id = ?", [outcome.signal_id])
        self.conn.execute(
            f"INSERT INTO signal_outcome ({_SIGNAL_OUTCOME_COLS}) "
            f"VALUES ({', '.join('?' * len(_SIGNAL_OUTCOME_COLS.split(', ')))})",
            [
                outcome.signal_id,
                outcome.computed_at,
                outcome.timeframe,
                outcome.acted,
                outcome.capacity_blocked,
                outcome.open_positions,
                outcome.max_positions,
                outcome.risk_per_share,
                outcome.horizon_bars,
                outcome.bars_seen,
                outcome.mae_r,
                outcome.mfe_r,
                outcome.mfe_bar,
                outcome.forward_r,
                outcome.resolution,
            ],
        )

    def measured_ids(self) -> set[int]:
        rows = self.conn.execute("SELECT signal_id FROM signal_outcome").fetchall()
        return {int(r[0]) for r in rows}

    def list_all(self) -> list[SignalOutcome]:
        rows = self.conn.execute(
            f"SELECT {_SIGNAL_OUTCOME_COLS} FROM signal_outcome ORDER BY signal_id"
        ).fetchall()
        return [_row_to_signal_outcome(r) for r in rows]

    def joined(
        self, limit: int | None = None, acted: bool | None = None
    ) -> list[tuple[SignalOutcome, EngineSignal, Instrument]]:
        n_out = len(_SIGNAL_OUTCOME_COLS.split(", "))
        n_sig = len(_SIGNAL_COLS.split(", "))
        where = "" if acted is None else "WHERE o.acted = ?"
        params: list[object] = [] if acted is None else [acted]
        if limit:
            params.append(limit)
        rows = self.conn.execute(
            f"""
            SELECT {", ".join(f"o.{c}" for c in _SIGNAL_OUTCOME_COLS.split(", "))},
                   {", ".join(f"s.{c}" for c in _SIGNAL_COLS.split(", "))},
                   i.id, i.symbol, i.name, i.type, i.exchange, i.sector, i.currency, i.tradable
            FROM signal_outcome o
            JOIN engine_signal s ON s.id = o.signal_id
            JOIN instrument i ON i.id = s.instrument_id
            {where}
            ORDER BY s.fired_at DESC, s.id DESC
            {"LIMIT ?" if limit else ""}
            """,
            params,
        ).fetchall()
        return [
            (
                _row_to_signal_outcome(r[:n_out]),
                _row_to_signal(r[n_out : n_out + n_sig]),
                _row_to_instrument(r[n_out + n_sig :]),
            )
            for r in rows
        ]

    def open_at(self, account_id: int, moment: datetime) -> int:
        """How many positions were open at an instant.

        Reconstructed from the positions themselves rather than remembered at the
        time: it is the only way to ask the question of history that predates the
        question. A trade counts as open if it was entered before the instant and
        had not closed by it.
        """
        row = self.conn.execute(
            """
            SELECT count(*) FROM engine_position
            WHERE account_id = ? AND opened_at <= ?
              AND (closed_at IS NULL OR closed_at > ?)
            """,
            [account_id, moment, moment],
        ).fetchone()
        return int(row[0]) if row else 0


def as_decimal(value: float | Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(str(value))
