import math
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, cast

import pandas as pd
import yfinance as yf

from trd.errors import ProviderError
from trd.models import DailyBar, EarningsDate, InstrumentInfo, InstrumentType, IntradayBar, Quote

# What yfinance will actually serve, and how far back it serves it. Asking for a
# wider window returns whatever exists; asking for an interval outside this map is
# a caller bug, so it raises rather than silently fetching a different timeframe.
INTRADAY_LOOKBACK_DAYS: dict[str, int] = {"1m": 7, "5m": 59, "15m": 59, "30m": 59, "1h": 729}

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


def _int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return int(f)


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
            year_high = _dec(getattr(fast, "year_high", None))
            year_low = _dec(getattr(fast, "year_low", None))
            volume = _int(getattr(fast, "last_volume", None))
            avg_volume = _int(getattr(fast, "three_month_average_volume", None))
        except Exception as exc:
            raise ProviderError(f"Quote fetch failed for {symbol}: {exc}") from exc
        if price is None:
            raise ProviderError(f"No price available for {symbol}")
        return Quote(
            symbol=symbol,
            price=price,
            prev_close=prev_close,
            year_high=year_high,
            year_low=year_low,
            volume=volume,
            avg_volume=avg_volume,
        )

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
                    adj_close=_dec(row.get("Adj Close")),
                )
            )
        return bars

    def get_intraday_bars(
        self, symbol: str, interval: str, start: date, end: date
    ) -> list[IntradayBar]:
        symbol = symbol.upper()
        if interval not in INTRADAY_LOOKBACK_DAYS:
            raise ProviderError(
                f"Unsupported intraday interval {interval!r} — "
                f"expected one of {', '.join(sorted(INTRADAY_LOOKBACK_DAYS))}"
            )
        # Clamping here, not at the call site: the limit is the provider's, and a
        # start older than it makes yfinance return an empty frame for the whole
        # request rather than the portion it does have.
        earliest = end - timedelta(days=INTRADAY_LOOKBACK_DAYS[interval])
        start = max(start, earliest)
        if start >= end:
            return []
        try:
            df = yf.Ticker(symbol).history(
                start=start.isoformat(), end=end.isoformat(), interval=interval, auto_adjust=False
            )
        except Exception as exc:
            raise ProviderError(f"Intraday fetch failed for {symbol}: {exc}") from exc
        if df is None or df.empty:
            return []
        bars: list[IntradayBar] = []
        for ts, row in df.iterrows():
            close = _dec(row.get("Close"))
            if close is None:
                continue
            stamp = cast(pd.Timestamp, ts)
            bars.append(
                IntradayBar(
                    # Naive exchange-local time: the bar's identity is its place in
                    # the session, and a tz-aware value would land in DuckDB as UTC
                    # and shift every stored bar by the offset of the day it was
                    # fetched on.
                    ts=stamp.tz_localize(None).to_pydatetime()
                    if stamp.tzinfo is not None
                    else stamp.to_pydatetime(),
                    open=_dec(row.get("Open")) or close,
                    high=_dec(row.get("High")) or close,
                    low=_dec(row.get("Low")) or close,
                    close=close,
                    volume=_int(row.get("Volume")),
                )
            )
        return bars

    def get_earnings_dates(self, symbol: str) -> list[EarningsDate]:
        symbol = symbol.upper()
        try:
            df = yf.Ticker(symbol).get_earnings_dates(limit=12)
        except ImportError as exc:
            # Missing optional dep (lxml) is an environment bug, not "no earnings" — surface it.
            raise ProviderError(f"Earnings fetch broken: {exc}") from exc
        except Exception:
            # Crypto/ETFs have no earnings; yfinance raises or returns junk. Treat as none.
            return []
        if df is None or df.empty:
            return []
        events: list[EarningsDate] = []
        for ts, row in df.iterrows():
            events.append(
                EarningsDate(
                    date=cast(pd.Timestamp, ts).date(),
                    eps_estimate=_dec(row.get("EPS Estimate")),
                    eps_actual=_dec(row.get("Reported EPS")),
                )
            )
        return events
