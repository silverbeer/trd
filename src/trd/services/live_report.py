"""The live engine's own post-market message: the account, then every position.

The combined daily report gives a live engine one block of totals, which answers
"am I up" and nothing after it. Real money provokes the next question straight
away — which names, how far from their stops, what moved today — and answering
it meant `trd engine positions` on a terminal. This is that answer, once a day,
in the private chat.

Built from the pieces that already exist, never beside them: the account block
is `engine_day` (so capital, worth and return mean exactly what the daily report
says they mean), the stop in force and the risk at it are `PositionRow` (so the
per-position number and `engine positions` cannot disagree), and P&L and R are
`EnginePosition`'s own arithmetic. What is new is the day's move, which needs the
previous session's close and nothing else.

No network call, like `status()`: marks are the last stored close, and a stale
one is said at the top rather than averaged into a confident number.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, computed_field

from trd.errors import TrdError
from trd.models import IntentStatus, PositionRow, Side
from trd.services.daily_report import EngineDay, ExitEvent, engine_day
from trd.services.engine import EngineService

# How far back to look for the session before the one being reported. A long
# weekend plus a holiday is four days; this leaves room without reading history.
PREVIOUS_SESSION_LOOKBACK_DAYS = 10

# Intents that ended without trading. Reported when they ended on the session:
# each is an order the engine wanted that never reached the book.
UNFILLED_STATUSES = (IntentStatus.REJECTED, IntentStatus.EXPIRED)


class LivePosition(BaseModel):
    """One open position, with everything a person checks before trusting it."""

    symbol: str
    strategy: str
    opened_at: datetime
    # Sessions the engine has counted while it held the trade (bars on 1d).
    sessions_held: int = 0
    opened_today: bool = False
    quantity: Decimal
    entry_price: Decimal
    # The last stored close and the session it belongs to. None = no bar at all.
    mark: Decimal | None = None
    marked_on: date | None = None
    # What today's move is measured from: the previous session's close, or the
    # entry price for a position opened today (it was not held at yesterday's
    # close, so a move from there is money it never had). None when the mark is
    # not today's — a day change off a stale bar would be yesterday's move.
    day_base: Decimal | None = None
    unrealized: Decimal | None = None
    unrealized_pct: Decimal | None = None
    r_now: Decimal | None = None
    initial_stop: Decimal
    stop_in_force: Decimal
    target: Decimal
    risk_at_stop: Decimal | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trailing(self) -> bool:
        """The chandelier stop has overtaken the initial one — the ↑ in `engine
        positions`."""
        return self.stop_in_force > self.initial_stop

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day_change(self) -> Decimal | None:
        if self.mark is None or self.day_base is None:
            return None
        return (self.mark - self.day_base) * self.quantity

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day_change_pct(self) -> Decimal | None:
        if self.mark is None or not self.day_base:
            return None
        return (self.mark - self.day_base) / self.day_base * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stop_distance_pct(self) -> Decimal | None:
        """How far price can fall from the mark before the stop in force fires,
        as a percentage of the mark. Negative = already through it."""
        if not self.mark:
            return None
        return (self.mark - self.stop_in_force) / self.mark * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def target_distance_pct(self) -> Decimal | None:
        if not self.mark:
            return None
        return (self.target - self.mark) / self.mark * 100


class OpenedToday(BaseModel):
    """A buy that filled on the session: the position it opened, at the fill."""

    symbol: str
    strategy: str
    quantity: Decimal
    entry_price: Decimal
    opened_at: datetime
    still_open: bool = True


class UnfilledIntent(BaseModel):
    """An order the engine wanted that ended the session without trading."""

    symbol: str
    side: Side
    quantity: Decimal
    status: IntentStatus
    rule: str | None = None
    strategy: str
    error: str | None = None


class ClosedToday(BaseModel):
    """A trade that closed on the session, and what of it belongs to today."""

    trade: ExitEvent
    # What today's move is measured from — the previous close, or the entry for a
    # trade opened and closed on the same session. The trade's P&L is from entry;
    # only the part since yesterday's close changed the account today.
    day_base: Decimal | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day_change(self) -> Decimal | None:
        trade = self.trade
        if self.day_base is None or trade.exit_price is None or trade.quantity is None:
            return None
        return (trade.exit_price - self.day_base) * trade.quantity


class LiveReport(BaseModel):
    """A live engine's session, position by position."""

    on: date
    day: EngineDay
    positions: list[LivePosition] = []
    opened_today: list[OpenedToday] = []
    closed_today: list[ClosedToday] = []
    unfilled_today: list[UnfilledIntent] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def market_open(self) -> bool:
        """Whether the engine has a bar for the session. Same test as the daily
        report: no bar, no session, and nothing is sent."""
        return self.day.last_session == self.on

    @computed_field  # type: ignore[prop-decorator]
    @property
    def today_change(self) -> Decimal | None:
        """How much the account's worth moved on the session: each open
        position's move since its day base, plus the closed trades' part of
        today. None when any piece is unknown — a partial sum would read as the
        whole day."""
        parts = [p.day_change for p in self.positions] + [c.day_change for c in self.closed_today]
        if any(part is None for part in parts):
            return None
        return sum((part for part in parts if part is not None), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def today_change_pct(self) -> Decimal | None:
        """Today's move against what the account was worth at yesterday's close."""
        change, worth = self.today_change, self.day.live_worth
        if change is None or worth is None or worth - change == 0:
            return None
        return change / (worth - change) * 100


def live_report(service: EngineService, name: str, on: date) -> LiveReport:
    """Build the live report for one engine on `on`. Refuses a paper engine:
    its numbers are already in the daily report, and a message headed "real
    money" over simulated fills would be the one lie this report cannot tell."""
    config = service.config()
    if not config.is_live:
        raise TrdError(
            "This is a paper engine. 'trd engine live-report' covers a LIVE engine; "
            "paper engines are in 'trd engine daily-report'."
        )
    day = engine_day(service, name, on)
    account = service.account()

    positions: list[LivePosition] = []
    for position, instrument in service.positions.list_open(account.id):
        dated = service.prices.latest_close_dated(instrument.id)
        marked_on, mark = dated if dated is not None else (None, None)
        row = PositionRow(position=position, instrument=instrument, price=mark)
        opened_today = position.opened_at.date() == on
        base: Decimal | None = None
        if marked_on == on:
            base = (
                position.entry_price
                if opened_today
                else _previous_close(service, instrument.id, on)
            )
        positions.append(
            LivePosition(
                symbol=instrument.symbol,
                strategy=position.strategy,
                opened_at=position.opened_at,
                sessions_held=position.bars_held,
                opened_today=opened_today,
                quantity=position.remaining_quantity,
                entry_price=position.entry_price,
                mark=mark,
                marked_on=marked_on,
                day_base=base,
                unrealized=position.pnl_at(mark),
                unrealized_pct=position.pnl_pct_at(mark),
                r_now=position.r_multiple_at(mark),
                initial_stop=position.stop_price,
                stop_in_force=row.stop_in_force,
                target=position.target_price,
                risk_at_stop=row.risk_at_stop,
            )
        )
    # Best first, in R where the trade has one, the same ordering the daily
    # report gives its exits: the top line is the best trade on the book.
    positions.sort(key=_rank, reverse=True)

    opened = [
        OpenedToday(
            symbol=instrument.symbol,
            strategy=position.strategy,
            quantity=position.quantity,
            entry_price=position.entry_price,
            opened_at=position.opened_at,
            still_open=position.closed_at is None,
        )
        for position, instrument in service.positions.list_all(account.id)
        if position.opened_at.date() == on
    ]
    opened.sort(key=lambda o: o.opened_at)

    ids = {i.symbol: i.id for _, i in service.positions.list_all(account.id)}
    closed = []
    for trade in day.exits_today:
        same_session = trade.opened_at is not None and trade.opened_at.date() == on
        instrument_id = ids.get(trade.symbol)
        if same_session:
            base = trade.entry_price
        elif instrument_id is not None:
            base = _previous_close(service, instrument_id, on)
        else:
            base = None
        closed.append(ClosedToday(trade=trade, day_base=base))

    unfilled = [
        UnfilledIntent(
            symbol=row.instrument.symbol,
            side=row.intent.side,
            quantity=row.intent.quantity,
            status=row.intent.status,
            rule=row.intent.rule,
            strategy=row.intent.strategy,
            error=row.intent.error,
        )
        for row in reversed(service.intent_rows())
        if row.intent.status in UNFILLED_STATUSES
        and (row.intent.resolved_at or row.intent.created_at).date() == on
    ]

    return LiveReport(
        on=on,
        day=day,
        positions=positions,
        opened_today=opened,
        closed_today=closed,
        unfilled_today=unfilled,
    )


def _previous_close(service: EngineService, instrument_id: int, on: date) -> Decimal | None:
    """The last stored close strictly before `on` — the session the day's move
    is measured from. Unadjusted, like the mark it is compared with."""
    closes = service.prices.closes_in_range(
        instrument_id,
        on - timedelta(days=PREVIOUS_SESSION_LOOKBACK_DAYS),
        on - timedelta(days=1),
        adjusted=False,
    )
    return closes[-1][1] if closes else None


def _rank(position: LivePosition) -> Decimal:
    if position.r_now is not None:
        return position.r_now
    return (position.unrealized or Decimal(0)) / Decimal(1000)
