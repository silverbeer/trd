from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, computed_field

from trd.models.core import Instrument, Side
from trd.timeframes import INTRADAY_MINUTES, day_mode_on_daily_bars


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class SizingMode(StrEnum):
    """How a trade's size is decided.

    EXPOSURE commits the same dollars per trade, so risk floats with the stop
    distance. RISK risks the same dollars per trade, so the committed capital
    floats instead and every R-multiple is worth the same amount of money.
    """

    EXPOSURE = "exposure"
    RISK = "risk"


class EngineMode(StrEnum):
    """Who fills the engine's decisions.

    PAPER fills them itself, in a simulation account, at the price the scan saw.
    LIVE trades a real account and only records order intents: the fill is the
    broker's, arrives later, and is ingested separately (see OrderIntent).
    """

    PAPER = "paper"
    LIVE = "live"


class EngineConfig(BaseModel):
    """How the engine is wired: which account it trades, which watchlist it
    scans, how much it commits per trade, and which rules are switched on."""

    id: int
    account_id: int
    watchlist: str
    position_size: Decimal
    max_positions: int
    strategies: list[str]
    exit_params: dict[str, float]
    # No new entry when a known earnings date falls within this many days. 0 is off.
    earnings_blackout_days: int = 3
    # What `position_size` means: dollars committed, or dollars risked.
    sizing_mode: SizingMode = SizingMode.EXPOSURE
    # The bar width the rules run on. '1d' reads price_daily; an intraday value
    # reads price_intraday, which is what makes a stop reachable inside a session.
    timeframe: str = "1d"
    # Most new entries the engine may open in one session. 0 is off. Caps the
    # *flow* of trades, where max_positions caps the stock of them — a day engine
    # recycles its slots as exits fire, so the two are different limits.
    max_entries_per_day: int = 0
    # The cash this engine trades with. None is off: no settled-cash ledger, and
    # capacity is the slot count alone. Set, an entry also needs settled cash —
    # sale proceeds settle T+1 in a cash account (see trd.engine.settlement).
    capital: Decimal | None = None
    # Paper fills its own trades; live writes order intents for a real account.
    mode: EngineMode = EngineMode.PAPER

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_intraday(self) -> bool:
        return self.timeframe != "1d"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live(self) -> bool:
        return self.mode == EngineMode.LIVE


class EngineSignal(BaseModel):
    """One entry signal. Stored whether or not it was acted on — the signals the
    engine passed over are half the learning."""

    id: int
    run_id: int | None = None
    instrument_id: int
    strategy: str
    # The instant the signal's bar opened — midnight for a daily bar, the bucket
    # for an intraday one. Identity, not display: it is what stops a monitor loop
    # from recording the same signal on every pass.
    bar_ts: datetime
    fired_at: datetime
    price: Decimal
    score: float
    reason: str
    acted: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bar_date(self) -> date:
        """The session the signal belongs to, for anything that groups by day."""
        return self.bar_ts.date()


class EnginePosition(BaseModel):
    """A virtual position plus its exit state machine.

    `stop_price` is the *initial* stop and never moves; the trailing stop is
    derived from `trail_high`. That keeps risk-per-share constant, so a closed
    trade's R-multiple means what it should.
    """

    id: int
    account_id: int
    instrument_id: int
    signal_id: int | None = None
    strategy: str
    opened_at: datetime
    entry_price: Decimal
    quantity: Decimal
    stop_price: Decimal
    target_price: Decimal
    atr_at_entry: Decimal
    trail_high: Decimal
    bars_held: int = 0
    last_bar_date: date | None = None
    status: PositionStatus = PositionStatus.OPEN
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    # What the rule said, and which rule said it. Both, because they answer
    # different questions: the prose explains one trade, the key is the only
    # thing a day's exits can be grouped by. NULL on trades closed before
    # migration 021 — a key guessed from a sentence would read as a recorded one.
    exit_reason: str | None = None
    exit_rule: str | None = None
    # How much of the original size has been sold, and the cash it booked.
    # `quantity` stays the size taken at entry so the R denominator never moves.
    closed_quantity: Decimal = Decimal(0)
    booked_pnl: Decimal = Decimal(0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining_quantity(self) -> Decimal:
        """What is still on. Exits sell this, not the original size."""
        return self.quantity - self.closed_quantity

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_partial(self) -> bool:
        """Some has been sold and some is still running."""
        return self.closed_quantity > 0 and self.remaining_quantity > 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk_per_share(self) -> Decimal:
        """Entry minus the initial stop — one R, the unit every result is measured in."""
        return self.entry_price - self.stop_price

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cost(self) -> Decimal:
        return self.entry_price * self.quantity

    def pnl_at(self, price: Decimal | None) -> Decimal | None:
        """Unrealised P&L on what is still held — the part a price can still move."""
        if price is None:
            return None
        return (price - self.entry_price) * self.remaining_quantity

    def pnl_pct_at(self, price: Decimal | None) -> Decimal | None:
        if price is None or self.entry_price == 0:
            return None
        return (price - self.entry_price) / self.entry_price * 100

    def r_multiple_at(self, price: Decimal | None) -> Decimal | None:
        """Result in units of initial risk. +2R = made twice what was risked."""
        if price is None or self.risk_per_share <= 0:
            return None
        return (price - self.entry_price) / self.risk_per_share

    def book_exit(self, quantity: Decimal, price: Decimal) -> None:
        """Record a sale of `quantity` at `price` against this position.

        The one place a partial or final exit is accounted, so the live engine and
        the backtest cannot drift on how a scaled-out trade is scored. `quantity`
        (the original size) is never touched — R is measured against what was
        risked at entry.
        """
        prior = self.closed_quantity
        self.booked_pnl += (price - self.entry_price) * quantity
        self.closed_quantity = prior + quantity
        self.exit_price = (
            price
            if prior <= 0 or self.exit_price is None
            else ((self.exit_price * prior) + price * quantity) / (prior + quantity)
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realized_pnl(self) -> Decimal | None:
        """Cash actually taken, summed over every exit this position has had.

        For a trade that closed in one go this is exactly what it always was:
        (exit - entry) x quantity. A scaled-out trade adds each piece as it goes.
        """
        if self.closed_quantity <= 0:
            return None
        return self.booked_pnl

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realized_r(self) -> Decimal | None:
        """Booked result in units of the risk taken at entry.

        The denominator is deliberately the *original* size: R measures the trade
        against what it risked, not against whatever is left of it. Sell 90% at
        +2R and stop the last 10% at -1R and this reads +1.7R — one number for one
        trade, which is what makes the scorecard mean anything.
        """
        if self.closed_quantity <= 0 or self.risk_per_share <= 0 or self.quantity <= 0:
            return None
        return self.booked_pnl / (self.risk_per_share * self.quantity)


class IntentStatus(StrEnum):
    """Where an order intent is in its life. The first three are open."""

    PENDING = "pending"  # written by a scan, not yet handed to the broker
    SUBMITTED = "submitted"  # placed; the broker has an order
    AWAITING_APPROVAL = "awaiting_approval"  # placed, held for a trade approval
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    EXPIRED = "expired"  # an approval nobody gave before it lapsed


# An open intent still claims what it asked for: a buy holds a slot and its cash,
# a sell blocks a second exit on the same position.
OPEN_INTENT_STATUSES = (
    IntentStatus.PENDING,
    IntentStatus.SUBMITTED,
    IntentStatus.AWAITING_APPROVAL,
)

# Open intents that may lapse unanswered: nothing at the broker has accepted them
# yet. SUBMITTED is not here — that one has a broker order, and only the broker's
# answer (filled, cancelled, rejected) may resolve it.
EXPIRABLE_INTENT_STATUSES = (IntentStatus.PENDING, IntentStatus.AWAITING_APPROVAL)

# Minute-of-day (ET) an unplaced or unapproved intent stops being actionable. A
# trade approval lapses at the 16:00 close of the day it was raised (measured
# 2026-10-21: expires_at 20:00Z on an approval created that morning), and a
# pending intent nobody placed by the close was sized on a session that is over.
INTENT_EXPIRY_MINUTE = 16 * 60


def intent_expiry_cutoff(now: datetime) -> datetime:
    """Open intents created before this instant are stale at `now`.

    Before the close it is today's midnight: anything from an earlier session has
    lapsed, today's are still live. From 16:00 it is tomorrow's midnight, so the
    day's own intents lapse too. Naive exchange-local, like every trd timestamp.
    """
    midnight = datetime.combine(now.date(), datetime.min.time())
    if now.hour * 60 + now.minute >= INTENT_EXPIRY_MINUTE:
        return midnight + timedelta(days=1)
    return midnight


# Minute-of-day (ET) from which a new intent is "late": placed now, an approval
# has minutes, not hours, before the 16:00 expiry above takes it.
INTENT_LATE_MINUTE = 15 * 60 + 45

# A pending buy older than this was sized off a quote from a different market.
# The outbox withholds it and the next live scan expires it, so the slot and the
# cash it reserved go back to a fresh plan instead of sitting idle until 16:00.
# Sells are exempt: their size is the position, not a quote.
STALE_INTENT_MINUTES = 30


def intent_expires_at(created_at: datetime) -> datetime:
    """The instant an intent stops being actionable: the 16:00 close of the session
    it was written in. The same boundary `intent_expiry_cutoff` sweeps on."""
    midnight = datetime.combine(created_at.date(), datetime.min.time())
    return midnight + timedelta(minutes=INTENT_EXPIRY_MINUTE)


def intent_is_late(created_at: datetime) -> bool:
    """Written so close to the close that an approval may not be given in time."""
    return created_at.hour * 60 + created_at.minute >= INTENT_LATE_MINUTE


class OrderIntent(BaseModel):
    """An order a live engine decided to place, and what became of it.

    Written by the scan in place of a fill. A buy carries the entry plan it was
    sized with, so the position can be opened at the REAL fill price with the stop
    re-derived from `atr_at_entry` — live R is measured from what was paid. A sell
    carries the exit rule that wanted it and the position it would close; the
    position stays open until the fill says otherwise.
    """

    id: int
    account_id: int
    instrument_id: int
    side: Side
    quantity: Decimal
    ref_price: Decimal  # the price the scan saw, not a fill
    notional: Decimal
    reason: str
    rule: str | None = None
    strategy: str
    signal_id: int | None = None
    position_id: int | None = None
    stop_price: Decimal | None = None
    target_price: Decimal | None = None
    atr_at_entry: Decimal | None = None
    # Ours, and the broker's idempotency key: re-sending cannot make two orders.
    ref_id: str
    status: IntentStatus = IntentStatus.PENDING
    # With trade approvals on, the broker answers with an approval whose id the
    # eventual order carries as its ref_id — ours is not what comes back.
    approval_id: str | None = None
    broker_order_id: str | None = None
    created_at: datetime
    submitted_at: datetime | None = None
    resolved_at: datetime | None = None
    error: str | None = None
    filled_quantity: Decimal | None = None
    fill_price: Decimal | None = None
    fees: Decimal | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_open(self) -> bool:
        return self.status in OPEN_INTENT_STATUSES


class OrderIntentRow(BaseModel):
    """An order intent joined to its instrument, for display."""

    intent: OrderIntent
    instrument: Instrument


class HaltCause(StrEnum):
    """Who stopped a live engine. MAX_LOSS is the engine stopping itself, which is
    the one a watchdog has to shout about: nobody chose it."""

    MANUAL = "manual"  # trd engine halt
    MAX_LOSS = "max_loss"  # realized + unrealized reached -max_loss


class EngineGuard(BaseModel):
    """A live engine's kill switch and hard caps, enforced by trd itself.

    Independent of the strategy config on purpose: a cap is a statement about
    the account, not about the rules, and it must hold whatever the rules (or a
    later reconfigure) say. Every cap applies to BUYS only — a cap that could
    withhold an exit would leave a position unmanaged, which is the risk the caps
    exist to bound. None is that cap off.
    """

    account_id: int
    halted: bool = False
    halt_reason: str | None = None
    halted_at: datetime | None = None
    halted_by: HaltCause | None = None
    max_order: Decimal | None = None  # dollars: one buy order's notional
    max_daily: Decimal | None = None  # dollars: buy notional per session
    max_positions: int | None = None  # open positions + open buy intents
    max_loss: Decimal | None = None  # dollars: realized + unrealized floor
    # False when nothing is stored and these are the defaults derived from the
    # config — what `init --live` would have written.
    stored: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def auto_halted(self) -> bool:
        """Halted by the engine's own loss stop rather than by a person."""
        return self.halted and self.halted_by == HaltCause.MAX_LOSS


class EngineRun(BaseModel):
    id: int
    started_at: datetime
    scanned: int = 0
    signals: int = 0
    opened: int = 0
    closed: int = 0
    paper: bool = True
    note: str | None = None


class SignalRow(BaseModel):
    """A stored signal joined to its instrument, for display."""

    signal: EngineSignal
    instrument: Instrument


class PositionRow(BaseModel):
    """An engine position joined to its instrument and a live price."""

    position: EnginePosition
    instrument: Instrument
    price: Decimal | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mark(self) -> Decimal | None:
        """Exit price for a closed trade, live price for an open one."""
        if self.position.status == PositionStatus.CLOSED:
            return self.position.exit_price
        return self.price

    @computed_field  # type: ignore[prop-decorator]
    @property
    def trail_stop(self) -> Decimal:
        """Where the chandelier stop currently sits. Never below the initial stop."""
        return self.position.trail_high - self.position.atr_at_entry * Decimal("3")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stop_in_force(self) -> Decimal:
        """The stop actually protecting the trade right now.

        The chandelier stop once it has overtaken the initial one, the initial
        stop until then — the same rule `TrailingStop` applies, so what is shown
        is what would fire.
        """
        return max(self.position.stop_price, self.trail_stop)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def risk_at_stop(self) -> Decimal | None:
        """Dollars lost from here if the stop in force is hit.

        Not the same thing as committed capital: a $1,000 position with a stop
        6% below the mark is $60 at risk, not $1,000. Measured from the *mark*
        rather than the entry, because that is the money still on the table —
        a trade already up 20% is no longer risking what it started with.

        Floored at zero: a position trading below its own stop between scans has
        no further protected downside to report, and a negative "risk" would
        quietly cancel out real risk elsewhere in the total.
        """
        if self.position.status == PositionStatus.CLOSED or self.mark is None:
            return None
        return max(Decimal(0), (self.mark - self.stop_in_force) * self.position.remaining_quantity)


class EngineStatus(BaseModel):
    """Everything you need to know before trusting a running engine, in one object.

    Assembled deliberately without a network call, so it answers even when the
    provider is down — and cheaply, so it never holds the database long.
    """

    build: str
    db_path: str
    account: str
    watchlist: str
    universe: list[str]
    strategies: list[str]
    position_size: Decimal
    max_positions: int
    earnings_blackout_days: int
    flat_at_minute: int
    timeframe: str = "1d"
    # Per-session entry budget. 0 = off, which is every engine that predates it.
    max_entries_per_day: int = 0
    # How much of that budget today has already used. Meaningless when the budget
    # is off, and rendered only when it is on.
    entries_today: int = 0
    # Market-regime gate on new entries. 0 = off, which is the default.
    regime_sma: int = 0
    regime_vix_max: float = 0.0
    # The T+1 settled-cash ledger. capital None = off, and then all four are None.
    # Settled is buying power; unsettled is sale proceeds a cash account may not
    # spend until the next settle date.
    capital: Decimal | None = None
    cash_settled: Decimal | None = None
    cash_unsettled: Decimal | None = None
    cash_next_settle: date | None = None
    # Paper or live, and how many order intents are still open (live only: a
    # paper engine never writes one).
    mode: EngineMode = EngineMode.PAPER
    open_intents: int = 0
    # The buys among them. Each holds a slot until its fill opens the position.
    open_buy_intents: int = 0
    # Placed and held at the broker for a person to approve.
    awaiting_approval: int = 0
    # Live only. An intent the broker has held past the session it was placed in,
    # or an approval past its 16:00 expiry that no sweep has resolved: either way
    # something between trd and the broker stopped moving (STUCK_SUBMITTED_SESSIONS
    # in trd.services.guard). Published in status.json for the watchdog.
    stuck_intents: int = 0
    # The kill switch and hard caps (live only; None on paper). The four halt
    # fields are flattened out of `guard` because they are what a reader, the
    # watchdog and the bot look for first.
    halted: bool = False
    halt_reason: str | None = None
    halted_at: datetime | None = None
    halted_by: HaltCause | None = None
    guard: EngineGuard | None = None

    open_positions: int
    closed_trades: int = 0
    committed: Decimal
    unrealized: Decimal
    # Cash actually booked, over every completed trade *and* every partial exit
    # taken out of a position that is still running. Without it, "am I up?" is a
    # two-command question with arithmetic in the middle.
    realized: Decimal = Decimal(0)
    # What is lost if every stop in force is hit. Always far below `committed`,
    # and the only number that says how the risk is distributed.
    risk_at_stop: Decimal = Decimal(0)
    marks_are_stale: bool  # marked at last stored close, not a live quote
    # The oldest session any open position is marked against. A number without
    # its date can't be judged: `engine reconcile` already prints the date of the
    # close it compared, for exactly this reason.
    marked_at: date | None = None

    bars_total: int
    bars_first: date | None
    bars_last: date | None
    warmup_bars: int
    short_history: list[tuple[str, int]]  # (symbol, bars) below what a rule needs
    # (strategy, sessions scanned since it last fired a signal — taken or not)
    # for every configured strategy quiet past SILENT_STRATEGY_SESSIONS. A rule
    # that can no longer fire reads as a quiet market from every other view:
    # pullback on the day engine sat at zero signals for two months after its
    # RSI lookback became 1,092 five-minute bars (SB-1341).
    silent_strategies: list[tuple[str, int]] = []

    last_scan: datetime | None
    scans_today: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day_mode(self) -> bool:
        return self.flat_at_minute > 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def config_refused(self) -> str | None:
        """Why this engine's own configuration is one `init` would now reject.

        The guard runs at init, so an engine created before it keeps running in a
        state the code calls a bug and nothing says so. Status is where that has
        to surface: it is the command reached for when results look wrong, and
        "every trade exits on the clock" looks like a flat strategy, not a
        misconfiguration.
        """
        refused = day_mode_on_daily_bars(self.timeframe, self.flat_at_minute)
        if refused is None:
            return None
        return (
            f"{refused} This engine is already running that way — rebuild it on an "
            f"intraday timeframe ({'/'.join(INTRADAY_MINUTES)}). Until then its "
            "R-multiples describe risk it never took."
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def regime_gated(self) -> bool:
        """True when any regime switch is on. A gate that silently blocks every
        entry is indistinguishable from a quiet market until you can see it."""
        return self.regime_sma > 0 or self.regime_vix_max > 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bar_unit(self) -> str:
        """What one bar is, for anything that counts them at the user."""
        return "day" if self.timeframe == "1d" else self.timeframe

    @computed_field  # type: ignore[prop-decorator]
    @property
    def capacity(self) -> int:
        return max(0, self.max_positions - self.open_positions - self.open_buy_intents)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def net_pnl(self) -> Decimal:
        """Everything the engine has made or lost, booked and on paper.

        Shown next to its two halves and never instead of them: an engine up on
        net only because of open positions, while most of its completed trades
        lost money, is a different engine from one that is up on both.
        """
        return self.realized + self.unrealized


class ExitOutlook(BaseModel):
    """One exit rule as it currently stands against a live position."""

    rule: str
    name: str
    level: Decimal | None = None  # the price that would trigger it, where there is one
    detail: str  # what it is waiting for, in plain English
    in_force: bool = False  # the stop actually protecting the trade right now


class TradeExplanation(BaseModel):
    """Why a trade was taken, what the words mean, and where it gets out.

    The engine's design rule is that a rule you cannot explain does not ship.
    This extends that to the trades themselves: every entry already records the
    numbers that fired it, and this joins them to the vocabulary needed to read
    them.
    """

    symbol: str
    strategy: str
    strategy_name: str
    strategy_description: str
    opened_at: datetime
    reason: str  # the recorded signal reason, with its numbers
    score: float | None = None
    entry_price: Decimal
    quantity: Decimal
    price: Decimal | None = None
    r_multiple: Decimal | None = None
    bars_held: int
    risk_per_share: Decimal
    glossary: list[tuple[str, str, str]] = []  # (key, term, definition)
    exits: list[ExitOutlook] = []


class TradeOutcome(BaseModel):
    """What one closed trade did while it was on — the shape, not just the result.

    Every ratio is in R, and `risk_per_share` is carried so any of them can be
    re-derived by hand. The bar indices matter as much as the values: an MFE on
    bar 2 of a forty-bar hold says the exit rule is slow, while the same MFE on
    the last bar says it is not.
    """

    position_id: int
    computed_at: datetime
    timeframe: str
    bars_seen: int
    risk_per_share: Decimal

    # Worst it ever looked, in R. Negative by construction on a trade that ever
    # traded below its entry; 0 on one that never did.
    mae_r: Decimal | None = None
    mae_at: datetime | None = None
    mae_bar: int | None = None

    # Best it ever looked, and when.
    mfe_r: Decimal | None = None
    mfe_at: datetime | None = None
    mfe_bar: int | None = None

    exit_r: Decimal | None = None
    # exit_r / mfe_r. None when nothing was ever offered — you cannot give back
    # what you never had, and a 0% there would read as an exit that failed.
    capture: Decimal | None = None

    # Where price went after the exit, in R from the exit price. Positive means
    # it kept working without us.
    follow_through_r: Decimal | None = None
    follow_through_bars: int | None = None
    # How many bars were actually available. A trade that closed yesterday has no
    # future yet, and zero bars must never read as zero movement.
    follow_through_seen: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def gave_back_r(self) -> Decimal | None:
        """How much of the best was handed back before the exit fired."""
        if self.mfe_r is None or self.exit_r is None:
            return None
        return self.mfe_r - self.exit_r

    @computed_field  # type: ignore[prop-decorator]
    @property
    def exited_early(self) -> bool | None:
        """Whether price kept going our way after we left, by a whole R.

        A blunt flag on purpose: one trade proves nothing and the threshold is
        arbitrary, but across a hundred trades the share of them is exactly the
        question "are we exiting too early" in a form that can be counted.
        """
        if self.follow_through_r is None or not self.follow_through_seen:
            return None
        return self.follow_through_r >= Decimal(1)


class SignalOutcome(BaseModel):
    """What a signal did next — including, and especially, the ones not taken.

    `engine_signal` has recorded every signal since the engine existed and
    nothing has ever read the unacted ones. They are the only rows here that can
    say whether the rules filter junk or discard winners.

    `capacity_blocked` is what keeps the answer honest: most passed signals could
    not have been taken, because the book was full. Counting their returns as
    money left on the table would be fiction.
    """

    signal_id: int
    computed_at: datetime
    timeframe: str
    acted: bool
    capacity_blocked: bool
    open_positions: int | None = None
    max_positions: int | None = None

    # The stop `plan_entry` would have set at that bar — the same function the
    # live fill path uses, so the counterfactual R and the real R are one unit.
    risk_per_share: Decimal | None = None
    horizon_bars: int
    bars_seen: int

    mae_r: Decimal | None = None
    mfe_r: Decimal | None = None
    mfe_bar: int | None = None
    forward_r: Decimal | None = None
    # Which came first inside the horizon: "target", "stop", or "neither". An
    # average return would call a signal good on a path that ran -1.2R first —
    # a path the engine would have stopped out of and never seen the end of.
    resolution: str | None = None


class StrategyStat(BaseModel):
    """Closed-trade scorecard for one strategy — the whole point of the dry run."""

    strategy: str
    trades: int
    wins: int
    losses: int
    total_pnl: Decimal
    avg_win_pct: Decimal | None = None
    avg_loss_pct: Decimal | None = None
    avg_r: Decimal | None = None
    open_trades: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def win_rate(self) -> Decimal | None:
        if self.trades == 0:
            return None
        return Decimal(self.wins) / Decimal(self.trades) * 100

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expectancy_r(self) -> Decimal | None:
        """Average R per trade. Above 0 means the rule paid for its risk."""
        return self.avg_r
