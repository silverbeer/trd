from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, computed_field

from trd.models.core import Instrument


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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_intraday(self) -> bool:
        return self.timeframe != "1d"


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
    exit_reason: str | None = None

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
        if price is None:
            return None
        return (price - self.entry_price) * self.quantity

    def pnl_pct_at(self, price: Decimal | None) -> Decimal | None:
        if price is None or self.entry_price == 0:
            return None
        return (price - self.entry_price) / self.entry_price * 100

    def r_multiple_at(self, price: Decimal | None) -> Decimal | None:
        """Result in units of initial risk. +2R = made twice what was risked."""
        if price is None or self.risk_per_share <= 0:
            return None
        return (price - self.entry_price) / self.risk_per_share

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realized_pnl(self) -> Decimal | None:
        return self.pnl_at(self.exit_price)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def realized_r(self) -> Decimal | None:
        return self.r_multiple_at(self.exit_price)


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
    # Market-regime gate on new entries. 0 = off, which is the default.
    regime_sma: int = 0
    regime_vix_max: float = 0.0

    open_positions: int
    committed: Decimal
    unrealized: Decimal
    marks_are_stale: bool  # marked at last stored close, not a live quote

    bars_total: int
    bars_first: date | None
    bars_last: date | None
    warmup_bars: int
    short_history: list[tuple[str, int]]  # (symbol, bars) below what a rule needs

    last_scan: datetime | None
    scans_today: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def day_mode(self) -> bool:
        return self.flat_at_minute > 0

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
        return max(0, self.max_positions - self.open_positions)


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
