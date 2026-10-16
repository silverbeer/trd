"""Backtest fidelity (SB-1342): pair the replay's trades with the live ones and
split the gap into parts that sum to it exactly.

Reuses the exit what-if fixtures: a day-engine trade entered at 100 with a
10-wide stop, so every price reads directly as an R-multiple.
"""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from tests.test_backtest import run
from tests.test_exit_whatif import DAY, FIVE, OPENED, live, session
from trd.engine import REGISTRY as STRATEGIES
from trd.engine.base import Strategy, StrategyContext, StrategySignal
from trd.errors import TrdError
from trd.models import Bar, IntradayBar
from trd.services.backtest import BacktestTrade, EntryFill, FillMode
from trd.services.fidelity import decompose, pair

BAR = OPENED.replace(second=0)  # the replay enters at the bar's own stamp


def replayed(
    symbol: str = "AAA",
    at: datetime = BAR,
    r: str = "-1.1",
    strategy: str = "momentum",
    rule: str = "stop",
) -> BacktestTrade:
    return BacktestTrade(
        symbol=symbol,
        strategy=strategy,
        entry_at=at,
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        entry_reason="",
        exit_at=at + timedelta(minutes=10),
        exit_price=Decimal("89"),
        rule=rule,
        exit_reason="",
        pnl=Decimal(r) * 10,
        r_multiple=Decimal(r),
    )


# --------------------------------------------------------------------- pairing


def test_pairs_by_symbol_and_session_nearest_entry_first() -> None:
    """Two trades in one name on one session pair with their own counterparts,
    not with whichever happened to come first in the list."""
    early = live(opened=OPENED, pid=1)
    late = live(opened=OPENED + timedelta(hours=2), pid=2)
    r_late = replayed(at=BAR + timedelta(hours=2))
    r_early = replayed()
    matched, live_only, replay_only = pair([(early, "AAA"), (late, "AAA")], [r_late, r_early])
    assert {(p.id, t.entry_at.hour) for p, t in matched} == {(1, 10), (2, 12)}
    assert live_only == [] and replay_only == []


def test_a_different_symbol_or_session_never_pairs() -> None:
    other_day = replayed(at=BAR + timedelta(days=1))
    other_name = replayed(symbol="BBB")
    matched, live_only, replay_only = pair([(live(), "AAA")], [other_day, other_name])
    assert matched == []
    assert len(live_only) == 1
    assert len(replay_only) == 2


# -------------------------------------------------------------- decomposition


def test_the_parts_sum_to_the_gap_and_name_where_it_came_from() -> None:
    """Live stopped out at 89 (-1.1R). The replay took the same trade but its
    fill made it -0.6R, and also took a +2R trade the engine never did. The
    exit-replay of the live entry stops at 89 too, so the exit part is zero
    and the whole matched difference is the entry."""
    bars: list[Bar] = session({(10, 10): "89"})
    live_trades = [(live(), "AAA")]
    replay = [replayed(r="-0.6"), replayed(symbol="BBB", r="2")]

    parts, gap, residual, detail = decompose(
        live_trades, replay, lambda _id: bars, FIVE, DAY, FillMode.CLOSE
    )
    by = {p.label: p for p in parts}
    assert gap == pytest.approx(2 - 0.6 + 1.1)
    assert residual == pytest.approx(0)
    assert by["replay-only trades"].total_r == pytest.approx(2)
    assert by["live-only trades (subtracted)"].total_r == 0
    assert by["matched: entry (fill and timing)"].total_r == pytest.approx(0.5)
    assert by["matched: exit (bar-close replay vs live)"].total_r == pytest.approx(0)
    assert detail["matched"] == 1
    assert detail["replay_only"] == {"momentum": 1}


def test_an_exit_the_replay_reads_differently_is_the_exit_part() -> None:
    """Live left at 95 (-0.5R) on a rule the bar-close replay does not see; the
    replay of the same entry rides to the stop at 89. Same fill, different exit:
    -0.6R lands in the exit part, none in the entry part."""
    bars: list[Bar] = session({(10, 10): "89"})
    trade = live(exit_price="95", rule="session_close")
    parts, _gap, residual, _detail = decompose(
        [(trade, "AAA")], [replayed(r="-1.1")], lambda _id: bars, FIVE, DAY, FillMode.CLOSE
    )
    by = {p.label: p for p in parts}
    assert by["matched: exit (bar-close replay vs live)"].total_r == pytest.approx(-0.6)
    assert by["matched: entry (fill and timing)"].total_r == pytest.approx(0)
    assert residual == pytest.approx(0)


def test_a_live_trade_the_replay_never_took_is_subtracted() -> None:
    bars: list[Bar] = session({(10, 10): "89"})
    parts, gap, residual, detail = decompose(
        [(live(), "AAA")], [], lambda _id: bars, FIVE, DAY, FillMode.CLOSE
    )
    by = {p.label: p for p in parts}
    assert by["live-only trades (subtracted)"].total_r == pytest.approx(1.1)
    assert gap == pytest.approx(1.1)
    assert residual == pytest.approx(0)
    assert detail["live_only"] == {"momentum": 1}


# ------------------------------------------------------ the open entry fill


class _FormingSpy(Strategy):
    """Fires only on a bar that looks like a live scan's forming bar — no
    volume yet, every price its open — at one chosen instant."""

    key = "forming_spy"
    name = "spy"
    description = "test"
    trend_sessions = 1
    signal_sessions = 1

    def __init__(self, at: datetime) -> None:
        self.at = at

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        last = ctx.bars[-1]
        assert isinstance(last, IntradayBar)
        if last.ts != self.at or last.volume is not None or last.high != last.open:
            return None
        return StrategySignal(strategy=self.key, score=1.0, reason="forming bar seen")


def _bars(crash_at: datetime | None = None) -> list[Bar]:
    """Three sessions of 5-minute bars ranging 99-101 around 100 (ATR ~2, so
    the stop sits ~4 below), optionally with one bar that opens at 100 and
    closes at 80."""
    out: list[Bar] = []
    for day in range(3):
        t = datetime(2026, 8, 3 + day, 9, 30)
        while t.hour < 16:
            crash = t == crash_at
            out.append(
                IntradayBar(
                    ts=t,
                    open=Decimal(100),
                    high=Decimal(101),
                    low=Decimal(80 if crash else 99),
                    close=Decimal(80 if crash else 100),
                    volume=1_000,
                )
            )
            t += timedelta(minutes=5)
    return out


def test_an_open_fill_reads_a_forming_bar_and_fills_at_its_open(monkeypatch) -> None:
    at = datetime(2026, 8, 5, 11, 0)
    monkeypatch.setitem(STRATEGIES, "forming_spy", _FormingSpy(at))
    bars = _bars()

    opened = run(
        {"AAA": bars}, strategies=["forming_spy"], timeframe="5m", entry_fill=EntryFill.OPEN
    )
    assert opened.entry_fill is EntryFill.OPEN
    assert opened.open_at_end == 1  # the spy only fires on a forming bar

    closed = run({"AAA": bars}, strategies=["forming_spy"], timeframe="5m")
    assert closed.open_at_end == 0  # a complete bar never looks like one


def test_the_entry_bar_itself_is_probed_for_an_exit(monkeypatch) -> None:
    """Filled at the open, the trade lives through the rest of the bar. A bar
    that opens at 100 and closes at 80 stops it out on that bar's close — the
    next live scan's reading — not one bar later."""
    at = datetime(2026, 8, 5, 11, 0)
    monkeypatch.setitem(STRATEGIES, "forming_spy", _FormingSpy(at))
    result = run(
        {"AAA": _bars(crash_at=at)},
        strategies=["forming_spy"],
        timeframe="5m",
        entry_fill=EntryFill.OPEN,
        fill=FillMode.CLOSE,  # the day engine's exit fill
    )
    (trade,) = result.trades
    assert trade.entry_price == Decimal(100)
    assert trade.rule == "stop"
    assert trade.exit_at == at
    assert trade.exit_price == Decimal(80)
    assert trade.mae_r is not None and trade.mae_r < -1  # the bar was lived through


def test_an_open_fill_is_refused_on_daily_bars() -> None:
    from tests.test_backtest import breakout_series

    with pytest.raises(TrdError, match="intraday"):
        run({"AAA": breakout_series()}, entry_fill=EntryFill.OPEN)
