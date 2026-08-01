from collections.abc import Sequence
from datetime import date
from typing import Protocol

from trd.models import DailyBar, EarningsDate, InstrumentInfo, IntradayBar, Quote


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

    def get_intraday_bars(
        self, symbol: str, interval: str, start: date, end: date
    ) -> list[IntradayBar]:
        """Intraday OHLCV, inclusive of start, exclusive of end.

        Providers cap how far back intraday history goes — yfinance serves roughly
        60 days of 5-minute bars — so a caller asking for more gets what exists,
        not an error. Returns empty for a symbol with no intraday series rather
        than raising, the same way earnings does for an ETF.
        """
        ...

    def get_earnings_dates(self, symbol: str) -> list[EarningsDate]:
        """Known earnings dates, past and upcoming. Empty for instruments without
        earnings (crypto, most ETFs) — never raises for that case."""
        ...

    def get_earnings_dates_batch(self, symbols: Sequence[str]) -> dict[str, list[EarningsDate]]:
        """Batch earnings. Symbols that fail are omitted — never raises for one
        bad symbol, the same contract `get_quotes` keeps.

        Separate from the singular call for the same reason quotes are: one
        request per symbol is the whole cost, and a blackout that has to re-check
        many names mid-session cannot pay for them serially.
        """
        ...
