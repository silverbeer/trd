from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class InstrumentType(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    CRYPTO = "crypto"


class AccountType(StrEnum):
    REAL = "real"
    SIMULATION = "simulation"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class InstrumentInfo(BaseModel):
    """What the market data provider knows about a symbol. Used to create instruments."""

    symbol: str
    name: str | None = None
    type: InstrumentType = InstrumentType.STOCK
    exchange: str | None = None
    sector: str | None = None
    currency: str = "USD"


class Instrument(BaseModel):
    id: int
    symbol: str
    name: str | None = None
    type: InstrumentType
    exchange: str | None = None
    sector: str | None = None
    currency: str = "USD"


class Account(BaseModel):
    id: int
    name: str
    type: AccountType
    currency: str = "USD"


class Transaction(BaseModel):
    id: int
    account_id: int
    instrument_id: int
    side: Side
    quantity: Decimal
    price: Decimal
    fees: Decimal = Decimal(0)
    executed_at: datetime
    note: str | None = None


class Quote(BaseModel):
    symbol: str
    price: Decimal
    prev_close: Decimal | None = None
    year_high: Decimal | None = None
    year_low: Decimal | None = None
    volume: int | None = None
    avg_volume: int | None = None

    @property
    def day_change(self) -> Decimal | None:
        if self.prev_close is None:
            return None
        return self.price - self.prev_close

    @property
    def day_change_pct(self) -> Decimal | None:
        if self.prev_close is None or self.prev_close == 0:
            return None
        return (self.price - self.prev_close) / self.prev_close * 100

    @property
    def year_range_pct(self) -> Decimal | None:
        """Where price sits in the 52-week range: 0 = at low, 100 = at high."""
        if self.year_high is None or self.year_low is None:
            return None
        span = self.year_high - self.year_low
        if span == 0:
            return None
        return (self.price - self.year_low) / span * 100

    @property
    def volume_ratio(self) -> Decimal | None:
        """Today's volume vs average — >1 means heavier than usual."""
        if not self.volume or not self.avg_volume:
            return None
        return Decimal(self.volume) / Decimal(self.avg_volume)


class Watchlist(BaseModel):
    id: int
    name: str


class EarningsDate(BaseModel):
    """One earnings event as the provider reports it (instrument-agnostic)."""

    date: date
    eps_estimate: Decimal | None = None
    eps_actual: Decimal | None = None


class EarningsEvent(BaseModel):
    """An earnings event tied to a tracked instrument."""

    instrument: Instrument
    date: date
    eps_estimate: Decimal | None = None
    eps_actual: Decimal | None = None


class BoardRow(BaseModel):
    """One line of the watch board: instrument + live market read."""

    instrument: Instrument
    watchlist: str
    quote: Quote | None = None
    price_stale: bool = False
    next_earnings: date | None = None


class DailyBar(BaseModel):
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None = None


class Position(BaseModel):
    """Derived holding: FIFO-net quantity and cost basis, plus current market data."""

    instrument: Instrument
    quantity: Decimal
    cost_basis: Decimal
    price: Decimal | None = None
    prev_close: Decimal | None = None
    price_stale: bool = False

    @property
    def avg_cost(self) -> Decimal | None:
        if self.quantity == 0:
            return None
        return self.cost_basis / self.quantity

    @property
    def market_value(self) -> Decimal | None:
        if self.price is None:
            return None
        return self.price * self.quantity

    @property
    def unrealized_pl(self) -> Decimal | None:
        mv = self.market_value
        if mv is None:
            return None
        return mv - self.cost_basis

    @property
    def unrealized_pl_pct(self) -> Decimal | None:
        pl = self.unrealized_pl
        if pl is None or self.cost_basis == 0:
            return None
        return pl / self.cost_basis * 100

    @property
    def day_change(self) -> Decimal | None:
        if self.price is None or self.prev_close is None:
            return None
        return (self.price - self.prev_close) * self.quantity

    @property
    def day_change_pct(self) -> Decimal | None:
        if self.price is None or self.prev_close is None or self.prev_close == 0:
            return None
        return (self.price - self.prev_close) / self.prev_close * 100
