import duckdb

from trd.errors import TrdError
from trd.models import BoardRow, Quote
from trd.providers.base import MarketDataProvider
from trd.repos import EarningsRepo, InstrumentRepo, PriceRepo, WatchlistRepo

DEFAULT_WATCHLIST = "default"


class WatchlistService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.instruments = InstrumentRepo(conn)
        self.watchlists = WatchlistRepo(conn)
        self.prices = PriceRepo(conn)
        self.earnings = EarningsRepo(conn)

    def add(self, symbol: str, list_name: str = DEFAULT_WATCHLIST) -> bool:
        """Add symbol to a watchlist (creating list and instrument as needed).
        Returns False if it was already on the list."""
        instrument = self.instruments.get_by_symbol(symbol)
        if instrument is None:
            instrument = self.instruments.insert(self.provider.get_info(symbol))
        watchlist = self.watchlists.get_or_create(list_name)
        return self.watchlists.add_item(watchlist.id, instrument.id)

    def remove(self, symbol: str, list_name: str = DEFAULT_WATCHLIST) -> None:
        instrument = self.instruments.get_by_symbol(symbol)
        watchlist = self.watchlists.get_by_name(list_name)
        if instrument is None or watchlist is None:
            raise TrdError(f"{symbol.upper()} is not on watchlist '{list_name}'.")
        if not self.watchlists.remove_item(watchlist.id, instrument.id):
            raise TrdError(f"{symbol.upper()} is not on watchlist '{list_name}'.")

    def board(self, list_name: str | None = None) -> list[BoardRow]:
        """Quote board for one list (or all lists). Live quotes, snapshot fallback."""
        watchlist_id: int | None = None
        if list_name is not None:
            watchlist = self.watchlists.get_by_name(list_name)
            if watchlist is None:
                raise TrdError(f"No watchlist named '{list_name}'.")
            watchlist_id = watchlist.id

        items = self.watchlists.items(watchlist_id)
        symbols = list({instrument.symbol for _, instrument in items})
        quotes = self.provider.get_quotes(symbols)

        rows: list[BoardRow] = []
        for wl_name, instrument in items:
            quote = quotes.get(instrument.symbol)
            stale = False
            if quote is not None:
                self.prices.insert_snapshot(instrument.id, quote.price, quote.prev_close)
            else:
                snapshot = self.prices.latest_snapshot(instrument.id)
                if snapshot is not None:
                    quote = Quote(
                        symbol=instrument.symbol, price=snapshot[0], prev_close=snapshot[1]
                    )
                stale = True
            rows.append(
                BoardRow(
                    instrument=instrument,
                    watchlist=wl_name,
                    quote=quote,
                    price_stale=stale,
                    next_earnings=self.earnings.next_for_instrument(instrument.id),
                )
            )
        return rows
