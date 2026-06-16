"""Movers board — one ranked view of what's up or down across everything you
own and watch. Owned names carry P&L (you hold them); watch-only names show price
and the day move. The 'how much am I making/losing, at a glance' view.
"""

from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.providers.base import MarketDataProvider
from trd.services.portfolio import PortfolioService
from trd.services.watchlist import WatchlistService


class MoverRow(BaseModel):
    symbol: str
    owned: bool
    watched: bool
    price: Decimal | None = None
    day_pct: Decimal | None = None  # today's % move
    day_change: Decimal | None = None  # today's $ move (owned only)
    pl: Decimal | None = None  # cumulative unrealized $ (owned only)
    pl_pct: Decimal | None = None
    value: Decimal | None = None  # market value of the holding (owned only)
    spark: list[float] = []  # recent closes for a trend arrow


_SORTS = {
    "day": lambda r: r.day_pct if r.day_pct is not None else Decimal("-9999"),
    "pl": lambda r: r.pl if r.pl is not None else Decimal("-1e18"),
    "value": lambda r: r.value if r.value is not None else Decimal(0),
    "symbol": lambda r: r.symbol,
}


class MoversService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.portfolio = PortfolioService(conn, provider)
        self.watchlist = WatchlistService(conn, provider)

    def board(self, sort: str = "day", include_simulation: bool = False) -> list[MoverRow]:
        positions = self.portfolio.positions(include_simulation=include_simulation)
        sparks = self.portfolio.sparklines(positions)
        rows: list[MoverRow] = []
        owned: dict[str, MoverRow] = {}
        for p in positions:
            row = MoverRow(
                symbol=p.instrument.symbol,
                owned=True,
                watched=False,
                price=p.price,
                day_pct=p.day_change_pct,
                day_change=p.day_change,
                pl=p.unrealized_pl,
                pl_pct=p.unrealized_pl_pct,
                value=p.market_value,
                spark=sparks.get(p.instrument.symbol, []),
            )
            rows.append(row)
            owned[p.instrument.symbol] = row

        watched_symbols: set[str] = set()
        watch_seen: set[str] = set()
        for board_row in self.watchlist.board():
            symbol = board_row.instrument.symbol
            watched_symbols.add(symbol)
            if symbol in owned:
                continue  # owned row already holds the richer data
            if symbol in watch_seen:
                continue  # same symbol on multiple lists — one row is enough
            watch_seen.add(symbol)
            quote = board_row.quote
            rows.append(
                MoverRow(
                    symbol=symbol,
                    owned=False,
                    watched=True,
                    price=quote.price if quote else None,
                    day_pct=quote.day_change_pct if quote else None,
                )
            )
        for symbol in watched_symbols & set(owned):
            owned[symbol].watched = True

        reverse = sort != "symbol"
        rows.sort(key=_SORTS.get(sort, _SORTS["day"]), reverse=reverse)
        return rows
