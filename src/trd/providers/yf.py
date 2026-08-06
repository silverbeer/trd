import math
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _quote_workers() -> int:
    """How many quote requests to have in flight at once.

    Deliberately modest. The ceiling here is Yahoo's tolerance, not the machine's
    — these threads spend their lives blocked on a socket, so more of them buys
    nothing once the network is saturated and risks getting the whole scan rate
    limited. Eight turns a 16-symbol fetch from ~4s into well under one, which is
    already far below the five-minute scan cadence.

    Read per call, not at import: an import-time constant would freeze whatever
    the environment happened to be when the module first loaded, and could not be
    exercised by a test at all.
    """
    raw = os.environ.get("TRD_QUOTE_WORKERS")
    if raw is None:
        return 8
    try:
        value = int(raw)
    except ValueError:
        return 8
    return max(1, min(value, 32))


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
            day_high = _dec(getattr(fast, "day_high", None))
            day_low = _dec(getattr(fast, "day_low", None))
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
            day_high=day_high,
            day_low=day_low,
            volume=volume,
            avg_volume=avg_volume,
        )

    def get_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        """Batch quotes, fetched concurrently.

        One quote is one HTTP round trip and nothing else in a scan comes close:
        measured over 16 symbols, loading every stored bar from DuckDB took 0.042s
        and evaluating all 64 symbol x strategy pairs took 0.018s, against 3.92s
        of serial quote fetching. The work is entirely network wait, so threads
        are the right tool — no process pool, no async rewrite of the provider.

        Failures stay per-symbol: one bad ticker is omitted from the result, the
        same as the serial version, because a scan that drops one name is far
        better than a scan that does not happen.
        """
        wanted = list(dict.fromkeys(s.upper() for s in symbols))
        if not wanted:
            return {}
        if len(wanted) == 1:
            try:
                return {wanted[0]: self.get_quote(wanted[0])}
            except ProviderError:
                return {}

        quotes: dict[str, Quote] = {}
        with ThreadPoolExecutor(max_workers=min(len(wanted), _quote_workers())) as pool:
            futures = {pool.submit(self.get_quote, symbol): symbol for symbol in wanted}
            for future in as_completed(futures):
                try:
                    quote = future.result()
                except ProviderError:
                    continue
                quotes[futures[future]] = quote
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

    def get_earnings_dates_batch(self, symbols: Sequence[str]) -> dict[str, list[EarningsDate]]:
        """Concurrent, for the same reason `get_quotes` is: one request per symbol
        is the whole cost. Failures stay per-symbol so one bad ticker cannot stop
        the blackout from being refreshed for the rest."""
        wanted = list(dict.fromkeys(s.upper() for s in symbols))
        if not wanted:
            return {}

        out: dict[str, list[EarningsDate]] = {}
        with ThreadPoolExecutor(max_workers=min(len(wanted), _quote_workers())) as pool:
            futures = {pool.submit(self.get_earnings_dates, sym): sym for sym in wanted}
            for future in as_completed(futures):
                try:
                    out[futures[future]] = future.result()
                except ProviderError:
                    continue
        return out

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
