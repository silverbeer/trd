"""An engine's money, session by session, from its capital (SB-1447).

`trd equity` values holdings and nothing else, which is right for an account
whose cash lives somewhere trd does not track and wrong for an engine: every
sale's proceeds leave the holdings, so an engine that takes profit reads as a
loss on the day it does. An engine with a capital has a complete cash record —
the capital replayed through every fill, the same rows the settled-cash ledger
reads — so its worth on any session is that cash plus what it held, marked at
that session's close. A sale moves cash and holdings by the same amount and
changes the worth by nothing, which is the property the equity curve lacks.

Arithmetic only, over stored rows: no provider, no quote, read-only connection.
"""

from collections import defaultdict
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, computed_field

from trd.engine.settlement import ledger
from trd.errors import TrdError
from trd.models import Side, Transaction
from trd.services.engine import EngineService
from trd.services.fifo import realized_sales

# The benchmark a reader compares an engine against first. Read from stored
# bars only; a home that never synced it shows '—', never a guess.
BENCHMARK = "SPY"

# Quantities below this are a position fully sold, left over from rounding.
_DUST = Decimal("0.000001")


class PerformanceSession(BaseModel):
    """One session's close: what the engine was worth and what moved it."""

    date: date
    worth: Decimal
    day_change: Decimal
    day_change_pct: Decimal | None
    since_start: Decimal
    since_start_pct: Decimal
    drawdown_pct: Decimal  # vs the running peak (capital included), <= 0
    realized: Decimal  # P&L booked by the sells on this session
    buys: int
    sells: int
    held: list[str]
    benchmark_since_start_pct: Decimal | None = None
    # The newest session is today's: its marks are the latest stored bar, which
    # the close sync replaces after 16:05. Its numbers still move.
    provisional: bool = False


class EnginePerformance(BaseModel):
    account: str
    mode: str
    capital: Decimal
    started: date
    benchmark: str = BENCHMARK
    sessions: list[PerformanceSession]
    realized: Decimal
    won: int
    lost: int
    # Symbols held on a session with no close stored at or before it. Their
    # value is unknown, so a session that holds one is left out, never zeroed.
    unpriced: list[str] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worth(self) -> Decimal:
        return self.sessions[-1].worth if self.sessions else self.capital

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net(self) -> Decimal:
        return self.worth - self.capital

    @computed_field  # type: ignore[prop-decorator]
    @property
    def return_pct(self) -> Decimal:
        return self.net / self.capital * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unrealized(self) -> Decimal:
        return self.net - self.realized

    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_drawdown_pct(self) -> Decimal:
        return min((s.drawdown_pct for s in self.sessions), default=Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def best(self) -> PerformanceSession | None:
        return max(self.sessions, key=lambda s: s.day_change, default=None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def worst(self) -> PerformanceSession | None:
        return min(self.sessions, key=lambda s: s.day_change, default=None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def benchmark_return_pct(self) -> Decimal | None:
        return self.sessions[-1].benchmark_since_start_pct if self.sessions else None


def _pct(part: Decimal, whole: Decimal) -> Decimal | None:
    return part / whole * 100 if whole else None


def engine_performance(service: EngineService, today: date | None = None) -> EnginePerformance:
    """Every session since the engine's first fill, valued from its capital."""
    today = today or date.today()
    config = service.config()
    if config.capital is None:
        raise TrdError(
            "This engine has no capital, so its cash is not on record and its worth "
            "cannot be known — only its holdings ('trd equity'). Set one with "
            "'trd engine cash --set AMOUNT' (the cash the account started with)."
        )
    capital = config.capital
    account = service.account()
    txns = service.txns.list_chronological(account.id)
    if not txns:
        raise TrdError("No fills yet — nothing to measure. The first scan that buys starts it.")
    started = txns[0].executed_at.date()

    by_instrument: dict[int, list[Transaction]] = defaultdict(list)
    for txn in txns:
        by_instrument[txn.instrument_id].append(txn)
    # FIFO over each name's WHOLE history before any date filter: a sale matched
    # against a window's buys alone would invent its profit.
    realized_by_txn = {}
    for history in by_instrument.values():
        realized_by_txn.update(realized_sales(history))

    symbols = {}
    closes: dict[int, list[tuple[date, Decimal]]] = {}
    for instrument_id in by_instrument:
        instrument = service.instruments.get(instrument_id)
        symbols[instrument_id] = instrument.symbol if instrument else str(instrument_id)
        # Unadjusted: the cash is nominal dollars, so the holdings must be too.
        closes[instrument_id] = service.prices.closes_in_range(
            instrument_id, date.min, today, adjusted=False
        )

    sessions_on = sorted({d for series in closes.values() for d, _ in series if d >= started})
    bench = service.instruments.get_by_symbol(BENCHMARK)
    bench_closes = (
        dict(service.prices.closes_in_range(bench.id, date.min, today, adjusted=True))
        if bench is not None
        else {}
    )
    # The benchmark starts at the close BEFORE the engine's first session: the
    # engine's day one counts from its capital, so SPY's must count from the
    # same starting line.
    bench_base = max(((d, c) for d, c in bench_closes.items() if d < started), default=None)

    flows = [(t.executed_at.date(), t.side, t.quantity, t.price, t.fees) for t in txns]
    out: list[PerformanceSession] = []
    unpriced: set[str] = set()
    previous, peak = capital, capital
    for session in sessions_on:
        cash = ledger(capital, flows, session)
        held: dict[int, Decimal] = defaultdict(Decimal)
        for txn in txns:
            if txn.executed_at.date() > session:
                break
            held[txn.instrument_id] += txn.quantity if txn.side == Side.BUY else -txn.quantity
        value, names, missing = Decimal(0), [], False
        for instrument_id, quantity in held.items():
            if quantity <= _DUST:
                continue
            mark = next((c for d, c in reversed(closes[instrument_id]) if d <= session), None)
            if mark is None:
                unpriced.add(symbols[instrument_id])
                missing = True
                continue
            value += quantity * mark
            names.append(symbols[instrument_id])
        if missing:
            continue
        worth = cash.settled + cash.unsettled + value
        peak = max(peak, worth)
        todays = [t for t in txns if t.executed_at.date() == session]
        bench_pct = None
        if bench_base is not None and session in bench_closes:
            bench_pct = _pct(bench_closes[session] - bench_base[1], bench_base[1])
        out.append(
            PerformanceSession(
                date=session,
                worth=worth,
                day_change=worth - previous,
                day_change_pct=_pct(worth - previous, previous),
                since_start=worth - capital,
                since_start_pct=(worth - capital) / capital * 100,
                drawdown_pct=(worth - peak) / peak * 100,
                realized=sum(
                    (realized_by_txn[t.id].pnl for t in todays if t.side == Side.SELL),
                    Decimal(0),
                ),
                buys=sum(1 for t in todays if t.side == Side.BUY),
                sells=sum(1 for t in todays if t.side == Side.SELL),
                held=sorted(names),
                benchmark_since_start_pct=bench_pct,
                provisional=session == today,
            )
        )
        previous = worth

    closed = [p for p, _ in service.positions.list_closed(account.id)]
    booked = [p.realized_pnl for p in closed if p.realized_pnl is not None]
    return EnginePerformance(
        account=account.name,
        mode=config.mode.value,
        capital=capital,
        started=started,
        sessions=out,
        realized=sum((r.pnl for r in realized_by_txn.values()), Decimal(0)),
        won=sum(1 for b in booked if b > 0),
        lost=sum(1 for b in booked if b <= 0),
        unpriced=sorted(unpriced),
    )
