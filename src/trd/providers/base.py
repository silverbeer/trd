from collections.abc import Sequence
from datetime import date
from typing import Protocol

from trd.models import DailyBar, EarningsDate, InstrumentInfo, Quote


class MarketDataProvider(Protocol):
    """All market data flows through this interface.

    yfinance is the first implementation; if it breaks or we outgrow it,
    swap implementations without touching services or CLI.
    """

    def get_quote(self, symbol: str) -> Quote:
        """Current price + previous close. Raises ProviderError on failure."""
        ...

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Batch quotes. Symbols that fail are omitted — never raises for one bad symbol."""
        ...

    def get_info(self, symbol: str) -> InstrumentInfo:
        """Resolve symbol to instrument metadata. Raises ProviderError if unresolvable."""
        ...

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        """Daily OHLCV history, inclusive of start, exclusive of end."""
        ...

    def get_earnings_dates(self, symbol: str) -> list[EarningsDate]:
        """Known earnings dates, past and upcoming. Empty for instruments without
        earnings (crypto, most ETFs) — never raises for that case."""
        ...
