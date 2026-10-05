import duckdb

from trd.models import Instrument, InstrumentInfo

_COLS = "id, symbol, name, type, exchange, sector, currency, tradable"

# The same list qualified for a join. Five repos used to spell the seven columns
# out by hand and slice the result positionally, so adding `tradable` broke all
# of them at once — the same class of bug as the hardcoded 18 in `_list`.
INSTRUMENT_COLS = len(_COLS.split(", "))


# The profile (migration 024) is read only by this repo's own lookups. Joins keep
# selecting the core columns above: a list or a book never needs a paragraph of
# business description per row, and the positional slices in five repos stay put.
_PROFILE = "industry, country, summary"
_FULL = f"{_COLS}, {_PROFILE}"


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
        industry=row[8] if len(row) > 8 else None,
        country=row[9] if len(row) > 9 else None,
        summary=row[10] if len(row) > 10 else None,
    )


class InstrumentRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get(self, instrument_id: int) -> Instrument | None:
        row = self.conn.execute(
            f"SELECT {_FULL} FROM instrument WHERE id = ?", [instrument_id]
        ).fetchone()
        return _row_to_instrument(row) if row else None

    def get_by_symbol(self, symbol: str) -> Instrument | None:
        row = self.conn.execute(
            f"SELECT {_FULL} FROM instrument WHERE symbol = ?", [symbol.upper()]
        ).fetchone()
        return _row_to_instrument(row) if row else None

    def insert(self, info: InstrumentInfo) -> Instrument:
        row = self.conn.execute(
            f"""
            INSERT INTO instrument (symbol, name, type, exchange, sector, currency, tradable,
                                    industry, country, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING {_FULL}
            """,
            [
                info.symbol.upper(),
                info.name,
                info.type.value,
                info.exchange,
                info.sector,
                info.currency,
                info.tradable,
                info.industry,
                info.country,
                info.summary,
            ],
        ).fetchone()
        assert row is not None
        return _row_to_instrument(row)

    def enrich(self, symbol: str, info: InstrumentInfo) -> Instrument | None:
        """Fill in whatever profile the stored row is missing, from a fresh fetch.

        COALESCE keeps anything already stored: a later fetch that comes back thin
        must never blank a sector a good one recorded. Name rides along because an
        instrument created from a bare ticker has none either.
        """
        # No RETURNING: DuckDB rewrites an UPDATE ... RETURNING on a referenced
        # row as delete + insert and trips the foreign keys (12 point here). A
        # plain UPDATE of unindexed columns is done in place.
        self.conn.execute(
            """
            UPDATE instrument SET
                name = COALESCE(name, ?),
                sector = COALESCE(sector, ?),
                industry = COALESCE(industry, ?),
                country = COALESCE(country, ?),
                summary = COALESCE(summary, ?)
            WHERE symbol = ?
            """,
            [info.name, info.sector, info.industry, info.country, info.summary, symbol.upper()],
        )
        return self.get_by_symbol(symbol)

    def list_all(self) -> list[Instrument]:
        rows = self.conn.execute(f"SELECT {_FULL} FROM instrument ORDER BY symbol").fetchall()
        return [_row_to_instrument(r) for r in rows]
