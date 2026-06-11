import duckdb

from trd.models import Instrument, InstrumentInfo

_COLS = "id, symbol, name, type, exchange, sector, currency"


def _row_to_instrument(row: tuple) -> Instrument:
    return Instrument(
        id=row[0],
        symbol=row[1],
        name=row[2],
        type=row[3],
        exchange=row[4],
        sector=row[5],
        currency=row[6],
    )


class InstrumentRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get(self, instrument_id: int) -> Instrument | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM instrument WHERE id = ?", [instrument_id]
        ).fetchone()
        return _row_to_instrument(row) if row else None

    def get_by_symbol(self, symbol: str) -> Instrument | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM instrument WHERE symbol = ?", [symbol.upper()]
        ).fetchone()
        return _row_to_instrument(row) if row else None

    def insert(self, info: InstrumentInfo) -> Instrument:
        row = self.conn.execute(
            """
            INSERT INTO instrument (symbol, name, type, exchange, sector, currency)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id, symbol, name, type, exchange, sector, currency
            """,
            [
                info.symbol.upper(),
                info.name,
                info.type.value,
                info.exchange,
                info.sector,
                info.currency,
            ],
        ).fetchone()
        assert row is not None
        return _row_to_instrument(row)

    def list_all(self) -> list[Instrument]:
        rows = self.conn.execute(f"SELECT {_COLS} FROM instrument ORDER BY symbol").fetchall()
        return [_row_to_instrument(r) for r in rows]
