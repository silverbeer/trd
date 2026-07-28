from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from trd.models.core import Instrument


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


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


class EngineSignal(BaseModel):
    """One entry signal. Stored whether or not it was acted on — the signals the
    engine passed over are half the learning."""

    id: int
    run_id: int | None = None
    instrument_id: int
    strategy: str
    bar_date: date
    fired_at: datetime
    price: Decimal
    score: float
    reason: str
    acted: bool = False


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

    @property
    def risk_per_share(self) -> Decimal:
        """Entry minus the initial stop — one R, the unit every result is measured in."""
        return self.entry_price - self.stop_price

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

    @property
    def realized_pnl(self) -> Decimal | None:
        return self.pnl_at(self.exit_price)

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

    @property
    def mark(self) -> Decimal | None:
        """Exit price for a closed trade, live price for an open one."""
        if self.position.status == PositionStatus.CLOSED:
            return self.position.exit_price
        return self.price

    @property
    def trail_stop(self) -> Decimal:
        """Where the chandelier stop currently sits. Never below the initial stop."""
        return self.position.trail_high - self.position.atr_at_entry * Decimal("3")


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

    @property
    def win_rate(self) -> Decimal | None:
        if self.trades == 0:
            return None
        return Decimal(self.wins) / Decimal(self.trades) * 100

    @property
    def expectancy_r(self) -> Decimal | None:
        """Average R per trade. Above 0 means the rule paid for its risk."""
        return self.avg_r
