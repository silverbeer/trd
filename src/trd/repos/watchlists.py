import duckdb

from trd.models import Instrument, Watchlist
from trd.repos.instruments import _row_to_instrument


class WatchlistRepo:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn

    def get_by_name(self, name: str) -> Watchlist | None:
        row = self.conn.execute("SELECT id, name FROM watchlist WHERE name = ?", [name]).fetchone()
        return Watchlist(id=row[0], name=row[1]) if row else None

    def get_or_create(self, name: str) -> Watchlist:
        existing = self.get_by_name(name)
        if existing:
            return existing
        row = self.conn.execute(
            "INSERT INTO watchlist (name) VALUES (?) RETURNING id, name", [name]
        ).fetchone()
        assert row is not None
        return Watchlist(id=row[0], name=row[1])

    def list_all(self) -> list[Watchlist]:
        rows = self.conn.execute("SELECT id, name FROM watchlist ORDER BY name").fetchall()
        return [Watchlist(id=r[0], name=r[1]) for r in rows]

    def add_item(self, watchlist_id: int, instrument_id: int) -> bool:
        """Add instrument to list. Returns False if it was already there."""
        exists = self.conn.execute(
            "SELECT 1 FROM watchlist_item WHERE watchlist_id = ? AND instrument_id = ?",
            [watchlist_id, instrument_id],
        ).fetchone()
        if exists:
            return False
        self.conn.execute(
            "INSERT INTO watchlist_item (watchlist_id, instrument_id) VALUES (?, ?)",
            [watchlist_id, instrument_id],
        )
        return True

    def remove_item(self, watchlist_id: int, instrument_id: int) -> bool:
        """Remove instrument from list. Returns False if it wasn't there."""
        exists = self.conn.execute(
            "SELECT 1 FROM watchlist_item WHERE watchlist_id = ? AND instrument_id = ?",
            [watchlist_id, instrument_id],
        ).fetchone()
        if not exists:
            return False
        self.conn.execute(
            "DELETE FROM watchlist_item WHERE watchlist_id = ? AND instrument_id = ?",
            [watchlist_id, instrument_id],
        )
        return True

    def items(self, watchlist_id: int | None = None) -> list[tuple[str, Instrument]]:
        """(watchlist_name, instrument) pairs, optionally scoped to one list."""
        sql = """
            SELECT w.name, i.id, i.symbol, i.name, i.type, i.exchange, i.sector, i.currency
            FROM watchlist_item wi
            JOIN watchlist w ON w.id = wi.watchlist_id
            JOIN instrument i ON i.id = wi.instrument_id
            {where}
            ORDER BY w.name, i.symbol
        """
        if watchlist_id is None:
            rows = self.conn.execute(sql.format(where="")).fetchall()
        else:
            rows = self.conn.execute(
                sql.format(where="WHERE wi.watchlist_id = ?"), [watchlist_id]
            ).fetchall()
        return [(r[0], _row_to_instrument(r[1:])) for r in rows]
