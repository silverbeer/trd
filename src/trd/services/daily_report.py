"""One message a day, after the close: was it up or down, what worked, what did not.

The engine already pushes a Telegram message per fill. That is event noise — it
says a trade happened and never whether the day was good, which is how a system
that runs every day stops being read.

Three things shape this module.

**Facts, not judgement.** Every number here is arithmetic over stored rows.
Whether a decision was *good* needs measurements that do not exist yet (MAE,
MFE, capture) and belongs in the reasoning that comes after them.

**Two engines, two databases.** `EngineConfigRepo.get()` takes the most recent
config, so one database is one engine and "combined" means summing two reads.
Each engine is read through its own short-lived connection and reduced to an
`EngineDay`; the caller owns the connections, exactly as the CLI already does
for a single engine.

**It says when it does not know.** A report that quietly prints yesterday's
marks as today's is worse than no report: `status()` already knows whether the
open book is marked against a stale close, and that travels to the top of the
message rather than being averaged into a confident number.
"""

from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel, computed_field

from trd.models import EnginePosition, StrategyStat
from trd.services.engine import EngineService, strategy_stats

# How far back "what is working" looks. A month of sessions is long enough that
# one lucky trade does not crown a strategy and short enough that a rule which
# stopped working shows up while it still matters.
DEFAULT_WINDOW_DAYS = 30

# A strategy with two closed trades has an expectancy, not evidence. Below this
# it is not named as best or worst — a scorecard that crowns a coin flip is how
# a report starts costing more attention than it returns.
MIN_WINDOW_TRADES = 3


class ExitEvent(BaseModel):
    """One position the engine closed on the session being reported.

    `rule` is the stored `exit_reason`. Kept separate per exit rather than summed,
    because stop / target / trail / time / session_close mean entirely different
    things about a trade and lumping them is exactly the signal being looked for.
    """

    symbol: str
    strategy: str
    # The exit rule's key, from `exit_rule` — NULL on trades closed before
    # migration 021, which read as a plain "closed".
    rule: str | None = None
    # The rule's own words about this trade. Not rendered in the message (the
    # numbers inside it make every row unique, which is what stops it being
    # groupable) but carried in --json, where a reader wants the sentence.
    reason: str | None = None
    pnl: Decimal
    r_multiple: Decimal | None = None


class EngineDay(BaseModel):
    """One engine's day, reduced to what the report has to say about it."""

    engine: str
    account: str = ""
    # The newest session this engine has bars for. The market-open test: on a
    # holiday it is the previous trading day, and no engine has today.
    last_session: date | None = None

    # -- what happened today
    exits_today: list[ExitEvent] = []
    realized_today: Decimal = Decimal(0)

    # -- where the engine stands, from `status()`, which never calls the network
    open_positions: int = 0
    unrealized: Decimal = Decimal(0)
    realized_all: Decimal = Decimal(0)
    risk_at_stop: Decimal = Decimal(0)
    marks_are_stale: bool = False
    marked_at: date | None = None

    # -- what is working, over the trailing window
    window_days: int = DEFAULT_WINDOW_DAYS
    window_trades: int = 0
    best: StrategyStat | None = None
    worst: StrategyStat | None = None

    # An engine that could not be read at all. Never fatal: one broken home must
    # not cost the report the other engine's numbers.
    error: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net(self) -> Decimal:
        """Booked plus on-paper, the same total `engine status` reports. Shown
        beside its two halves and never instead of them."""
        return self.realized_all + self.unrealized

    @computed_field  # type: ignore[prop-decorator]
    @property
    def losses_today(self) -> list[ExitEvent]:
        return [e for e in self.exits_today if e.pnl < 0]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def wins_today(self) -> list[ExitEvent]:
        return [e for e in self.exits_today if e.pnl >= 0]


class DailyReport(BaseModel):
    """Every engine's day, plus the combined totals."""

    on: date
    window_days: int = DEFAULT_WINDOW_DAYS
    engines: list[EngineDay] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def market_open(self) -> bool:
        """Whether the session being reported actually traded.

        No trading calendar: if no engine has a bar for the date, the market did
        not open (or the sync has not landed one yet, which is what
        `marks_are_stale` is for). A report that posted "flat, nothing happened"
        every Thanksgiving would be training its reader to ignore it.
        """
        return any(e.last_session == self.on for e in self.engines)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stale(self) -> bool:
        return any(e.marks_are_stale for e in self.engines)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realized_today(self) -> Decimal:
        return sum((e.realized_today for e in self.engines), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exits_today(self) -> int:
        return sum(len(e.exits_today) for e in self.engines)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def unrealized(self) -> Decimal:
        return sum((e.unrealized for e in self.engines), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realized_all(self) -> Decimal:
        return sum((e.realized_all for e in self.engines), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net(self) -> Decimal:
        return self.realized_all + self.unrealized

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk_at_stop(self) -> Decimal:
        return sum((e.risk_at_stop for e in self.engines), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def open_positions(self) -> int:
        return sum(e.open_positions for e in self.engines)


def engine_day(
    service: EngineService,
    name: str,
    on: date,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> EngineDay:
    """Reduce one engine to its day. No network call — `status()` is deliberately
    offline, and everything else is arithmetic over closed trades, so the report
    still answers on an afternoon the provider is down.

    `on` selects the session whose exits are counted and where the trailing
    window ends. The open book (unrealized, money at risk) is always *current*:
    reconstructing a point-in-time book would need marks per day that the engine
    does not store, and a made-up number is worse than a labelled one.
    """
    status = service.status()
    account = service.account()

    cutoff = on - timedelta(days=window_days)
    exits: list[ExitEvent] = []
    window: list[EnginePosition] = []
    for position, instrument in service.positions.list_closed(account.id):
        if position.closed_at is None:
            continue
        closed_on = position.closed_at.date()
        if closed_on == on:
            exits.append(
                ExitEvent(
                    symbol=instrument.symbol,
                    strategy=position.strategy,
                    rule=position.exit_rule,
                    reason=position.exit_reason,
                    pnl=position.realized_pnl or Decimal(0),
                    r_multiple=position.realized_r,
                )
            )
        if cutoff < closed_on <= on:
            window.append(position)

    best, worst = _best_and_worst(window)
    return EngineDay(
        engine=name,
        account=account.name,
        last_session=status.bars_last,
        exits_today=sorted(exits, key=lambda e: e.pnl),
        # Only trades that closed today. A partial trim taken today on a position
        # still running books real cash and is NOT counted here — it lands in
        # `realized_all` (and so in NET) when it happens, and in this line only
        # when the trade finally closes. Counting it twice is the alternative.
        realized_today=sum((e.pnl for e in exits), Decimal(0)),
        open_positions=status.open_positions,
        unrealized=status.unrealized,
        realized_all=status.realized,
        risk_at_stop=status.risk_at_stop,
        marks_are_stale=status.marks_are_stale,
        marked_at=status.marked_at,
        window_days=window_days,
        window_trades=len(window),
        best=best,
        worst=worst,
    )


def _best_and_worst(
    closed: list[EnginePosition],
) -> tuple[StrategyStat | None, StrategyStat | None]:
    """The strategies at each end of the trailing window's expectancy.

    Same `strategy_stats` the scorecard and the backtest use, so "best strategy"
    in the report and in `trd engine report` can never mean two different sums.
    A strategy is only named once it has enough closed trades to have said
    anything — see MIN_WINDOW_TRADES.
    """
    ranked = [
        stat
        for stat in strategy_stats(closed)
        if stat.trades >= MIN_WINDOW_TRADES and stat.expectancy_r is not None
    ]
    if not ranked:
        return None, None
    ranked.sort(key=lambda s: s.expectancy_r or Decimal(0))
    best = ranked[-1]
    worst = ranked[0]
    # One strategy is not a comparison: naming the same rule as both the best and
    # the worst thing the engine did reads as a bug in the report.
    return best, (worst if worst.strategy != best.strategy else None)


def failed_engine(name: str, message: str) -> EngineDay:
    """An engine that could not be read, carried into the report rather than
    raising. Losing the swing engine's numbers because the day engine's home is
    unmounted is how a daily report stops being trusted."""
    return EngineDay(engine=name, error=message)
