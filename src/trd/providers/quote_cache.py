"""One quote fetch per symbol per pass, shared by every engine (SB-1509).

Four engines scan on the same cron minute, each quoting its own universe. The
universes overlap almost entirely — measured 2026-11-02: 616 quote fetches a
pass for 163 distinct symbols, fired as one burst from one address, every five
minutes. That is the load Yahoo answered with 429s.

The cache is a directory every scan pod mounts. A pass takes an exclusive lock,
reads what a sibling already fetched within the TTL, fetches only the rest,
writes the union back, and releases. So the first engine in pays for its
universe, the next three pay for the handful of names only they trade, and the
burst becomes one fetch of the union.

**Fail open, always.** No directory, a lock not granted in time, an unreadable
file: every one of those fetches directly, exactly as before the cache existed.
A quote cache that can stop a scan would put a performance fix in the trading
path. Only fresh quotes are served: a stale one is refetched, never returned.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from trd.models import Quote

ENV_DIR = "TRD_QUOTE_CACHE"
ENV_TTL = "TRD_QUOTE_CACHE_TTL"
# All four scans fire inside the same minute; a quote older than this belongs
# to an earlier pass and is refetched. Short on purpose: a scan marks stops and
# sizes entries off these prices.
DEFAULT_TTL_SECONDS = 60.0
# How long a pass waits for a sibling's fetch before giving up and fetching on
# its own. A full-universe fetch takes a few seconds; this is generous.
LOCK_TIMEOUT_SECONDS = 45.0
_POLL_SECONDS = 0.1
# Entries older than this are dropped on write, so the file stays the size of
# the union of the universes rather than every symbol ever quoted.
_KEEP_SECONDS = 3600.0
_FILE = "quotes.json"
_LOCK = "quotes.lock"

Fetch = Callable[[list[str]], dict[str, Quote]]


@dataclass
class QuoteCacheStats:
    """What one pass cost, for the scan log. The measurement SB-1509 asked for."""

    wanted: int = 0
    cached: int = 0
    fetched: int = 0
    waited_seconds: float = 0.0
    bypassed: str | None = None  # why the cache was not used, when it was not

    def line(self) -> str:
        if self.bypassed:
            return f"quote-cache: bypassed ({self.bypassed}); fetched {self.fetched}/{self.wanted}"
        return (
            f"quote-cache: {self.cached} cached, {self.fetched} fetched of {self.wanted} "
            f"(waited {self.waited_seconds:.1f}s)"
        )


class SharedQuoteCache:
    def __init__(
        self,
        directory: Path,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.directory = directory
        self.ttl = ttl_seconds
        self.lock_timeout = lock_timeout
        self.clock = clock

    def get_quotes(self, symbols: Sequence[str], fetch: Fetch) -> tuple[dict, QuoteCacheStats]:
        wanted = list(dict.fromkeys(s.upper() for s in symbols))
        stats = QuoteCacheStats(wanted=len(wanted))
        if not wanted:
            return {}, stats
        if not self.directory.is_dir():
            return self._direct(wanted, fetch, stats, "no cache directory")
        started = time.monotonic()
        try:
            with self._locked() as held:
                stats.waited_seconds = time.monotonic() - started
                if not held:
                    return self._direct(wanted, fetch, stats, "lock timeout")
                stored = self._read()
                now = self.clock()
                fresh: dict[str, Quote] = {}
                for symbol in wanted:
                    entry = stored.get(symbol)
                    if entry is None or now - entry[0] > self.ttl:
                        continue
                    fresh[symbol] = entry[1]
                missing = [s for s in wanted if s not in fresh]
                fetched = fetch(missing) if missing else {}
                stats.cached = len(fresh)
                stats.fetched = len(fetched)
                fetched_at = self.clock()
                for symbol, quote in fetched.items():
                    stored[symbol.upper()] = (fetched_at, quote)
                self._write(stored, fetched_at)
                return {**fresh, **fetched}, stats
        except OSError as exc:
            # A failure after the fetch would refetch here; rare, and correct.
            return self._direct(wanted, fetch, stats, f"io error: {exc}")

    # ------------------------------------------------------------------ helpers

    def _direct(
        self, wanted: list[str], fetch: Fetch, stats: QuoteCacheStats, why: str
    ) -> tuple[dict, QuoteCacheStats]:
        quotes = fetch(wanted)
        stats.bypassed = why
        stats.cached = 0
        stats.fetched = len(quotes)
        return quotes, stats

    @contextlib.contextmanager
    def _locked(self) -> Iterator[bool]:
        """Yields True holding the lock, False if it was not granted in time.

        fcntl record locks — the same kind DuckDB takes on these mounts, which
        is how a scan already sees another pod's writer lock.
        """
        fd = os.open(self.directory / _LOCK, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        yield False
                        return
                    time.sleep(_POLL_SECONDS)
            try:
                yield True
            finally:
                fcntl.lockf(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read(self) -> dict[str, tuple[float, Quote]]:
        try:
            raw = json.loads((self.directory / _FILE).read_text())
        except (OSError, ValueError):
            return {}  # absent or torn: start empty, the fetch rewrites it
        out: dict[str, tuple[float, Quote]] = {}
        if not isinstance(raw, dict):
            return out
        for symbol, entry in raw.items():
            try:
                out[symbol] = (float(entry["at"]), Quote.model_validate(entry["quote"]))
            except (KeyError, TypeError, ValueError):
                continue  # one bad row costs that symbol a fetch, nothing more
        return out

    def _write(self, stored: dict[str, tuple[float, Quote]], now: float) -> None:
        payload = {
            symbol: {"at": at, "quote": quote.model_dump(mode="json")}
            for symbol, (at, quote) in stored.items()
            if now - at <= _KEEP_SECONDS
        }
        path = self.directory / _FILE
        tmp = path.with_name(f"{_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)  # readers never see a half-written file


def quote_cache_from_env(env: dict[str, str] | None = None) -> SharedQuoteCache | None:
    """The cache when TRD_QUOTE_CACHE names a directory, else None (no caching).

    Unset everywhere but the scan pods, so a laptop, the tests and every other
    command keep fetching their own quotes.
    """
    source = os.environ if env is None else env
    raw = (source.get(ENV_DIR) or "").strip()
    if not raw:
        return None
    try:
        ttl = float(source.get(ENV_TTL) or DEFAULT_TTL_SECONDS)
    except ValueError:
        ttl = DEFAULT_TTL_SECONDS
    return SharedQuoteCache(Path(raw), ttl_seconds=ttl)
