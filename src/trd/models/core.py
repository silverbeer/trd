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
