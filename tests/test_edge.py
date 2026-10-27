"""Entry edge vs a random entry (SB-1344).

Synthetic 5-minute sessions, built so the answer is known by construction: a
random walk where the "taken" bars are themselves random must show no edge, and
a series where every taken bar sits just before a ramp must show one. A study of
whether the rules beat chance cannot be checked against the rules' own output.
"""

import json
import os
import random
from datetime import datetime, timedelta
from decimal import Decimal

from typer.testing import CliRunner

from trd.cli.app import app
from trd.db.connection import connect
from trd.engine.bars import BarSource
from trd.models import EngineSignal, IntradayBar
from trd.services.edge import eligible_bars, measure_edge, summarize
from trd.services.outcomes import risk_series

runner = CliRunner()
FIVE = BarSource.stamper("5m")
PARAMS = {"stop_atr_mult": 2.0, "target_r": 2.0, "flat_at_minute": 1555.0}
SESSIONS = [datetime(2026, 8, 3) + timedelta(days=d) for d in range(10)]
BARS_PER_SESSION = 78  # 09:30 .. 15:55


def _session_stamps() -> list[datetime]:
    return [
        day.replace(hour=9, minute=30) + timedelta(minutes=5 * i)
        for day in SESSIONS
        for i in range(BARS_PER_SESSION)
    ]


def _bars(closes: list[float]) -> list[IntradayBar]:
    out = []
    prev = closes[0]
    for stamp, close in zip(_session_stamps(), closes, strict=True):
        hi, lo = max(prev, close) + 0.1, min(prev, close) - 0.1
        out.append(
            IntradayBar(
                ts=stamp,
                open=Decimal(str(round(prev, 4))),
                high=Decimal(str(round(hi, 4))),
                low=Decimal(str(round(lo, 4))),
                close=Decimal(str(round(close, 4))),
                volume=1_000,
            )
        )
        prev = close
    return out


def _signal(n: int, bar: IntradayBar) -> EngineSignal:
    return EngineSignal(
        id=n,
        instrument_id=1,
        strategy="momentum" if n % 2 else "breakout",
        bar_ts=bar.ts,
        fired_at=bar.ts + timedelta(minutes=5),
        price=bar.close,
        score=0.5,
        reason="test",
        acted=True,
    )


def _random_walk(seed: int = 7) -> list[IntradayBar]:
    rng = random.Random(seed)
    price, closes = 100.0, []
    for _ in range(len(SESSIONS) * BARS_PER_SESSION):
        price += rng.gauss(0, 0.3)
        closes.append(price)
    return _bars(closes)


def _late_bars(bars: list[IntradayBar]) -> list[int]:
    """Bars after warmup and well inside the entry window."""
    return [
        i
        for i, b in enumerate(bars)
        if i > BARS_PER_SESSION and 600 <= b.ts.hour * 60 + b.ts.minute < 900
    ]


def test_random_signals_on_a_random_walk_show_no_edge() -> None:
    bars = _random_walk()
    pick = random.Random(99)
    signals = [_signal(n, bars[i]) for n, i in enumerate(pick.sample(_late_bars(bars), 200))]
    pairs, _ = measure_edge(signals, lambda _id: bars, FIVE, PARAMS, samples=20, seed=1)
    group = summarize("all", pairs)
    assert len(pairs) == 200
    for m in (group.net_r, group.forward_r, group.mfe_r):
        assert m.edge is not None and m.edge_se is not None
        assert abs(m.edge) < 3 * m.edge_se, m


def test_signals_ahead_of_a_ramp_show_a_positive_edge() -> None:
    """Flat noise, except a steady climb right after every 'taken' bar."""
    rng = random.Random(3)
    n = len(SESSIONS) * BARS_PER_SESSION
    ramps = set(range(BARS_PER_SESSION + 20, n - 20, 39))
    price, closes = 100.0, []
    for i in range(n):
        price += 0.6 if any(0 < i - r <= 10 for r in ramps) else rng.gauss(0, 0.05)
        closes.append(price)
    bars = _bars(closes)
    signals = [_signal(k, bars[r]) for k, r in enumerate(sorted(ramps))]
    pairs, _ = measure_edge(signals, lambda _id: bars, FIVE, PARAMS, samples=20, seed=1)
    group = summarize("all", pairs)
    assert group.net_r.edge is not None and group.net_r.edge > 0
    assert group.net_r.significant
    assert group.forward_r.significant and (group.forward_r.edge or 0) > 0


def test_the_seed_makes_the_draws_reproducible() -> None:
    bars = _random_walk()
    signals = [_signal(n, bars[i]) for n, i in enumerate(_late_bars(bars)[::25])]
    a, _ = measure_edge(signals, lambda _id: bars, FIVE, PARAMS, samples=10, seed=5)
    b, _ = measure_edge(signals, lambda _id: bars, FIVE, PARAMS, samples=10, seed=5)
    c, _ = measure_edge(signals, lambda _id: bars, FIVE, PARAMS, samples=10, seed=6)
    assert summarize("x", a).model_dump() == summarize("x", b).model_dump()
    assert summarize("x", a).model_dump() != summarize("x", c).model_dump()


def test_random_entries_respect_the_engines_entry_window() -> None:
    """No draw before the opening delay, none inside the cutoff before flat."""
    bars = _random_walk()
    params = {**PARAMS, "entry_delay_minutes": 30.0}
    risk = risk_series(bars, FIVE, params)
    pool = eligible_bars(bars, FIVE, SESSIONS[3].date(), params, risk)
    minutes = [bars[i].ts.hour * 60 + bars[i].ts.minute for i in pool]
    assert pool and min(minutes) == 10 * 60  # 09:30 + 30
    assert max(minutes) < 15 * 60 + 25  # 15:55 flat - 30 min cutoff
    assert {bars[i].ts.date() for i in pool} == {SESSIONS[3].date()}


def test_cli_with_an_empty_database_answers_with_a_json_error(tmp_path) -> None:
    home = tmp_path / "fresh"
    connect(home / "trd.duckdb").close()
    os.environ["TRD_HOME"] = str(home)
    try:
        result = runner.invoke(app, ["engine", "edge", "--json"])
    finally:
        os.environ.pop("TRD_HOME", None)
    assert result.exit_code != 0
    assert json.loads(result.stdout)["error"]
