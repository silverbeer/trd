"""The shared quote cache (SB-1509): one fetch per symbol per pass across engines."""

import json
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from trd.models import Quote
from trd.providers.quote_cache import SharedQuoteCache, quote_cache_from_env


class Counting:
    """A fetch that records what it was asked for. Symbols in `fail` get no quote."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail or set()

    def __call__(self, symbols):
        self.calls.append(list(symbols))
        return {
            s: Quote(symbol=s, price=Decimal("10.5"), prev_close=Decimal("10"))
            for s in symbols
            if s not in self.fail
        }

    @property
    def fetched(self) -> list[str]:
        return [s for call in self.calls for s in call]


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cache(tmp_path, clock) -> SharedQuoteCache:
    return SharedQuoteCache(tmp_path, ttl_seconds=60, clock=clock)


def test_overlapping_universes_fetch_each_symbol_once(cache) -> None:
    fetch = Counting()
    swing, stats_a = cache.get_quotes(["AAA", "BBB", "CCC"], fetch)
    live, stats_b = cache.get_quotes(["aaa", "BBB", "DDD"], fetch)
    assert sorted(fetch.fetched) == ["AAA", "BBB", "CCC", "DDD"]
    assert set(live) == {"AAA", "BBB", "DDD"}
    assert live["AAA"].price == Decimal("10.5")  # round-trips as Decimal
    assert (stats_a.cached, stats_a.fetched) == (0, 3)
    assert (stats_b.cached, stats_b.fetched) == (2, 1)
    assert set(swing) == {"AAA", "BBB", "CCC"}


def test_a_quote_older_than_the_ttl_is_fetched_again(cache, clock) -> None:
    fetch = Counting()
    cache.get_quotes(["AAA"], fetch)
    clock.now += 61
    _, stats = cache.get_quotes(["AAA"], fetch)
    assert fetch.fetched == ["AAA", "AAA"]
    assert stats.cached == 0


def test_a_symbol_the_provider_could_not_quote_is_not_cached(cache) -> None:
    fetch = Counting(fail={"BAD"})
    quotes, _ = cache.get_quotes(["AAA", "BAD"], fetch)
    assert set(quotes) == {"AAA"}
    cache.get_quotes(["AAA", "BAD"], fetch)
    assert fetch.calls[-1] == ["BAD"]  # retried, never served as missing


def test_no_directory_fetches_directly(tmp_path) -> None:
    fetch = Counting()
    quotes, stats = SharedQuoteCache(tmp_path / "gone").get_quotes(["AAA"], fetch)
    assert set(quotes) == {"AAA"}
    assert stats.bypassed == "no cache directory"
    assert not (tmp_path / "gone").exists()


def test_a_corrupt_file_costs_a_fetch_and_is_rewritten(cache, tmp_path) -> None:
    (tmp_path / "quotes.json").write_text("{not json")
    fetch = Counting()
    quotes, stats = cache.get_quotes(["AAA"], fetch)
    assert set(quotes) == {"AAA"} and stats.bypassed is None
    assert "AAA" in json.loads((tmp_path / "quotes.json").read_text())


def test_entries_long_past_the_ttl_are_dropped_on_write(cache, clock, tmp_path) -> None:
    fetch = Counting()
    cache.get_quotes(["OLD"], fetch)
    clock.now += 7200
    cache.get_quotes(["NEW"], fetch)
    assert set(json.loads((tmp_path / "quotes.json").read_text())) == {"NEW"}


def test_a_lock_held_too_long_falls_back_to_fetching(tmp_path) -> None:
    """Another pod holding the lock never stops a scan: it fetches on its own."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,os,sys,time\n"
            f"fd=os.open({str(tmp_path / 'quotes.lock')!r},os.O_RDWR|os.O_CREAT)\n"
            "fcntl.lockf(fd,fcntl.LOCK_EX)\n"
            "print('held',flush=True)\n"
            "time.sleep(10)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        fetch = Counting()
        started = time.monotonic()
        quotes, stats = SharedQuoteCache(tmp_path, lock_timeout=0.3).get_quotes(["AAA"], fetch)
        assert time.monotonic() - started < 5
        assert set(quotes) == {"AAA"}
        assert stats.bypassed == "lock timeout"
    finally:
        holder.kill()
        holder.wait()


def test_concurrent_processes_fetch_the_union_once(tmp_path) -> None:
    """Four scans in the same second: the lock makes them take turns."""
    log = tmp_path / "fetched.log"
    script = (
        "import sys\n"
        "from decimal import Decimal\n"
        "from pathlib import Path\n"
        "from trd.models import Quote\n"
        "from trd.providers.quote_cache import SharedQuoteCache\n"
        "def fetch(symbols):\n"
        "    import time; time.sleep(0.2)\n"
        f"    with open({str(log)!r},'a') as f: f.write(''.join(s+'\\n' for s in symbols))\n"
        "    return {s: Quote(symbol=s, price=Decimal('1')) for s in symbols}\n"
        f"SharedQuoteCache(Path({str(tmp_path)!r})).get_quotes(sys.argv[1].split(','), fetch)\n"
    )
    universes = ["AAA,BBB,CCC", "AAA,BBB,DDD", "AAA,BBB,CCC", "BBB,CCC,EEE"]
    procs = [subprocess.Popen([sys.executable, "-c", script, u]) for u in universes]
    for p in procs:
        assert p.wait(timeout=30) == 0
    assert sorted(log.read_text().split()) == ["AAA", "BBB", "CCC", "DDD", "EEE"]


def test_from_env() -> None:
    assert quote_cache_from_env({}) is None
    assert quote_cache_from_env({"TRD_QUOTE_CACHE": "  "}) is None
    cache = quote_cache_from_env({"TRD_QUOTE_CACHE": "/x", "TRD_QUOTE_CACHE_TTL": "30"})
    assert cache is not None and cache.directory == Path("/x") and cache.ttl == 30
    bad = quote_cache_from_env({"TRD_QUOTE_CACHE": "/x", "TRD_QUOTE_CACHE_TTL": "soon"})
    assert bad is not None and bad.ttl == 60


def test_scan_writes_its_quotes_to_the_shared_cache(cli_env, tmp_path, monkeypatch) -> None:
    from tests.test_daily_report import _home
    from trd.cli.app import app

    home = _home(tmp_path, "swing", cli_env)
    shared = tmp_path / "quote-cache"
    shared.mkdir()
    monkeypatch.setenv("TRD_HOME", str(home))
    monkeypatch.setenv("TRD_QUOTE_CACHE", str(shared))
    result = CliRunner().invoke(app, ["engine", "scan", "--json"])
    assert result.exit_code == 0, result.output
    assert "AAA" in json.loads((shared / "quotes.json").read_text())
