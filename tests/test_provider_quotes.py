"""Concurrent batch quotes.

Quote fetching is ~100% of a scan's cost and grows linearly with the universe, so
`get_quotes` runs its requests in parallel. These tests never touch the network:
`get_quote` is stubbed, and what is under test is the batching contract around it
— every symbol asked for, failures isolated, one request per distinct symbol.
"""

import threading
import time
from decimal import Decimal

import pytest

from trd.errors import ProviderError
from trd.models import Quote
from trd.providers.yf import YFinanceProvider, _quote_workers


class StubProvider(YFinanceProvider):
    """The real `get_quotes`, over a `get_quote` that never leaves the process."""

    def __init__(self, delay: float = 0.0, failing: set[str] | None = None) -> None:
        self.delay = delay
        self.failing = failing or set()
        self.calls: list[str] = []
        self.peak_concurrency = 0
        self._live = 0
        self._lock = threading.Lock()

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        with self._lock:
            self.calls.append(symbol)
            self._live += 1
            self.peak_concurrency = max(self.peak_concurrency, self._live)
        try:
            if self.delay:
                time.sleep(self.delay)
            if symbol in self.failing:
                raise ProviderError(f"No price available for {symbol}")
            return Quote(symbol=symbol, price=Decimal("100"))
        finally:
            with self._lock:
                self._live -= 1


SYMBOLS = ["AAPL", "NVDA", "AMD", "MU", "TSM", "META", "MSFT", "GOOGL"]


def test_every_symbol_comes_back() -> None:
    provider = StubProvider()
    quotes = provider.get_quotes(SYMBOLS)
    assert sorted(quotes) == sorted(SYMBOLS)
    assert all(q.price == Decimal("100") for q in quotes.values())


def test_symbols_are_upper_cased_like_the_serial_version() -> None:
    provider = StubProvider()
    assert sorted(provider.get_quotes(["aapl", "nVdA"])) == ["AAPL", "NVDA"]


def test_a_failing_symbol_is_omitted_not_raised() -> None:
    """A scan that drops one name beats a scan that does not happen."""
    provider = StubProvider(failing={"AMD"})
    quotes = provider.get_quotes(SYMBOLS)
    assert "AMD" not in quotes
    assert len(quotes) == len(SYMBOLS) - 1


def test_every_symbol_failing_yields_an_empty_result() -> None:
    provider = StubProvider(failing=set(SYMBOLS))
    assert provider.get_quotes(SYMBOLS) == {}


def test_no_symbols_does_no_work() -> None:
    provider = StubProvider()
    assert provider.get_quotes([]) == {}
    assert provider.calls == []


def test_a_duplicate_symbol_is_fetched_once() -> None:
    """The engine asks for the universe plus anything held, and a held name is
    usually in the universe too — that overlap should not cost a round trip."""
    provider = StubProvider()
    quotes = provider.get_quotes(["AAPL", "aapl", "NVDA", "AAPL"])
    assert sorted(quotes) == ["AAPL", "NVDA"]
    assert sorted(provider.calls) == ["AAPL", "NVDA"]


def test_a_single_symbol_skips_the_pool() -> None:
    provider = StubProvider()
    assert list(provider.get_quotes(["AAPL"])) == ["AAPL"]
    assert provider.calls == ["AAPL"]


def test_a_single_failing_symbol_is_still_empty_not_raised() -> None:
    provider = StubProvider(failing={"AAPL"})
    assert provider.get_quotes(["AAPL"]) == {}


def test_requests_actually_overlap() -> None:
    """The point of the change. Serial, eight 50ms quotes take 400ms; overlapped
    they take roughly one delay. Asserted on observed concurrency rather than
    wall clock alone, so a loaded CI machine cannot make it flap."""
    provider = StubProvider(delay=0.05)
    started = time.monotonic()
    provider.get_quotes(SYMBOLS)
    elapsed = time.monotonic() - started

    assert provider.peak_concurrency > 1
    assert elapsed < 0.05 * len(SYMBOLS) / 2


def test_worker_count_is_bounded() -> None:
    provider = StubProvider(delay=0.02)
    provider.get_quotes([f"SYM{i}" for i in range(40)])
    assert provider.peak_concurrency <= _quote_workers()


# --------------------------------------------------------------- worker count


def test_worker_count_defaults_without_configuration(monkeypatch) -> None:
    monkeypatch.delenv("TRD_QUOTE_WORKERS", raising=False)
    assert _quote_workers() == 8


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4", 4),
        ("1", 1),
        ("0", 1),  # a pool of zero would deadlock the scan
        ("-3", 1),
        ("999", 32),  # more sockets than Yahoo will tolerate buys nothing
        ("banana", 8),  # a typo must not take the engine down
        ("", 8),
    ],
)
def test_worker_count_is_clamped(monkeypatch, raw, expected) -> None:
    monkeypatch.setenv("TRD_QUOTE_WORKERS", raw)
    assert _quote_workers() == expected
