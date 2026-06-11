import duckdb

from trd.models import EarningsEvent
from trd.repos import EarningsRepo


class EarningsService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.earnings = EarningsRepo(conn)

    def upcoming(self, days: int = 14) -> list[EarningsEvent]:
        """Upcoming earnings across every tracked instrument (portfolio + watchlists)."""
        return self.earnings.upcoming(days)
