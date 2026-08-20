import duckdb

from trd.models import Instrument, InstrumentInfo

_COLS = "id, symbol, name, type, exchange, sector, currency, tradable"

# The same list qualified for a join. Five repos used to spell the seven columns
# out by hand and slice the result positionally, so adding `tradable` broke all
# of them at once — the same class of bug as the hardcoded 18 in `_list`.
INSTRUMENT_COLS = len(_COLS.split(", "))


def prefixed_cols(alias: str = "i") -> str:
    return ", ".join(f"{alias}.{c}" for c in _COLS.split(", "))


def _row_to_instrument(row: tuple) -> Instrument:
    return Instrument(
        id=row[0],
        symbol=row[1],
        name=row[2],
        type=row[3],
        exchange=row[4],
        sector=row[5],
        currency=row[6],
        # Tolerant of a short row and of NULL: a caller selecting the older seven
        # columns still gets a usable instrument, and rows written before the
        # migration backfilled them arrive as None rather than a boolean.
        tradable=True if len(row) < 8 or row[7] is None else row[7],
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
            INSERT INTO instrument (symbol, name, type, exchange, sector, currency, tradable)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id, symbol, name, type, exchange, sector, currency, tradable
            """,
            [
                info.symbol.upper(),
                info.name,
                info.type.value,
                info.exchange,
                info.sector,
                info.currency,
                info.tradable,
            ],
        ).fetchone()
        assert row is not None
        return _row_to_instrument(row)

    def list_all(self) -> list[Instrument]:
        rows = self.conn.execute(f"SELECT {_COLS} FROM instrument ORDER BY symbol").fetchall()
        return [_row_to_instrument(r) for r in rows]
