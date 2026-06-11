import json

import duckdb

from trd.models import IndicatorConfig

_COLS = "id, key, params, enabled, display_order, note"


def _row_to_config(row: tuple) -> IndicatorConfig:
    return IndicatorConfig(
        id=row[0],
        key=row[1],
        params=json.loads(row[2]) if isinstance(row[2], str) else row[2],
        enabled=row[3],
        display_order=row[4],
        note=row[5],
    )


class IndicatorConfigRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def add(
        self,
        key: str,
        params: dict,
        note: str | None = None,
        display_order: int | None = None,
    ) -> IndicatorConfig:
        if display_order is None:
            row = self.conn.execute(
                "SELECT coalesce(max(display_order), 0) + 1 FROM indicator_config"
            ).fetchone()
            assert row is not None
            display_order = row[0]
        row = self.conn.execute(
            f"""
            INSERT INTO indicator_config (key, params, display_order, note)
            VALUES (?, ?, ?, ?)
            RETURNING {_COLS}
            """,
            [key, json.dumps(params), display_order, note],
        ).fetchone()
        assert row is not None
        return _row_to_config(row)

    def list_enabled(self) -> list[IndicatorConfig]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM indicator_config WHERE enabled ORDER BY display_order, id"
        ).fetchall()
        return [_row_to_config(r) for r in rows]

    def list_all(self) -> list[IndicatorConfig]:
        rows = self.conn.execute(
            f"SELECT {_COLS} FROM indicator_config ORDER BY enabled DESC, display_order, id"
        ).fetchall()
        return [_row_to_config(r) for r in rows]

    def disable(self, config_id: int, reason: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE indicator_config
            SET enabled = false, disabled_at = current_timestamp,
                note = CASE WHEN ? IS NULL THEN note
                            ELSE coalesce(note || ' | ', '') || ? END
            WHERE id = ?
            """,
            [reason, reason, config_id],
        )

    def count(self) -> int:
        row = self.conn.execute("SELECT count(*) FROM indicator_config").fetchone()
        assert row is not None
        return row[0]
