import math
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any

import yfinance as yf

from trd.errors import ProviderError
from trd.models import DailyBar, InstrumentInfo, InstrumentType, Quote

_QUOTE_TYPE_MAP = {
    "EQUITY": InstrumentType.STOCK,
    "ETF": InstrumentType.ETF,
    "MUTUALFUND": InstrumentType.ETF,
    "CRYPTOCURRENCY": InstrumentType.CRYPTO,
}


def _dec(value: Any) -> Decimal | None:
    """Convert a provider float to Decimal; None for missing/NaN."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return Decimal(str(f))


class YFinanceProvider:
    """MarketDataProvider backed by yfinance (unofficial Yahoo Finance).

    Known fragility: Yahoo changes endpoints occasionally. Every upstream
    failure is wrapped in ProviderError so callers never see yfinance internals.
    """

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        try:
            fast = yf.Ticker(symbol).fast_info
            price = _dec(fast.last_price)
            prev_close = _dec(fast.previous_close)
        except Exception as exc:
            raise ProviderError(f"Quote fetch failed for {symbol}: {exc}") from exc
        if price is None:
            raise ProviderError(f"No price available for {symbol}")
        return Quote(symbol=symbol, price=price, prev_close=prev_close)

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        quotes: dict[str, Quote] = {}
        for symbol in symbols:
            try:
                quotes[symbol.upper()] = self.get_quote(symbol)
            except ProviderError:
                continue
        return quotes

    def get_info(self, symbol: str) -> InstrumentInfo:
        symbol = symbol.upper()
        try:
            info = yf.Ticker(symbol).get_info()
        except Exception as exc:
            raise ProviderError(f"Info fetch failed for {symbol}: {exc}") from exc
        if not info or info.get("quoteType") in (None, "NONE"):
            raise ProviderError(f"Symbol {symbol} not found")
        return InstrumentInfo(
            symbol=symbol,
            name=info.get("longName") or info.get("shortName"),
            type=_QUOTE_TYPE_MAP.get(info.get("quoteType", ""), InstrumentType.STOCK),
            exchange=info.get("fullExchangeName") or info.get("exchange"),
            sector=info.get("sector"),
            currency=info.get("currency") or "USD",
        )

    def get_daily_bars(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        symbol = symbol.upper()
        try:
            df = yf.Ticker(symbol).history(
                start=start.isoformat(), end=end.isoformat(), interval="1d", auto_adjust=False
            )
        except Exception as exc:
            raise ProviderError(f"History fetch failed for {symbol}: {exc}") from exc
        bars: list[DailyBar] = []
        for ts, row in df.iterrows():
            close = _dec(row.get("Close"))
            if close is None:
                continue
            volume = row.get("Volume")
            bars.append(
                DailyBar(
                    date=ts.date(),
                    open=_dec(row.get("Open")) or close,
                    high=_dec(row.get("High")) or close,
                    low=_dec(row.get("Low")) or close,
                    close=close,
                    volume=int(volume)
                    if volume is not None and not math.isnan(float(volume))
                    else None,
                )
            )
        return bars
