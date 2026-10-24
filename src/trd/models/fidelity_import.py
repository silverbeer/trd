"""A Fidelity CSV export, parsed, and the writes it implies (SB-1418).

Fidelity has no retail API, so a Fidelity-managed account is tracked from the
exports its website offers: Activity History (every buy, sell, distribution and
fee) and Positions (what is held now). These models are the parsed rows and the
plan of writes, so `--dry-run` and the real import print the same document.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, computed_field


class ActivityKind(StrEnum):
    """What an Activity History row IS, read from the start of its Action text."""

    BUY = "buy"
    SELL = "sell"
    REINVESTMENT = "reinvestment"
    DIVIDEND = "dividend"
    CAPITAL_GAIN_LONG = "capital_gain_long"
    CAPITAL_GAIN_SHORT = "capital_gain_short"
    ADVISORY_FEE = "advisory_fee"
    # Money the owner moved: a contribution, a rollover, a transfer, a journal.
    # Direction is the Amount's sign.
    TRANSFER = "transfer"


class ActivityRow(BaseModel):
    line: int  # 1-based line in the file, for error messages
    run_date: date
    action: str
    kind: ActivityKind
    symbol: str | None
    description: str | None
    price: Decimal | None
    quantity: Decimal  # as Fidelity writes it: a sell is negative
    fees: Decimal  # Commission ($) + Fees ($)
    amount: Decimal  # signed cash: a buy is negative
    cash_balance: Decimal | None
    settlement_date: date | None
    core: bool  # the core money market — cash, not a holding


class WriteTarget(StrEnum):
    TXN = "txn"
    INCOME = "income"
    CASH_FLOW = "cash_flow"
    # Recognised and deliberately not written: a core money market reinvestment
    # is cash staying cash.
    NONE = "none"


class PlannedWrite(BaseModel):
    """One write the import makes, or would make on --dry-run."""

    key: str  # the idempotency key (import_row.row_key)
    line: int | None  # None for the opening balance, which no row states
    on: date
    target: WriteTarget
    # buy | sell (txn); dividend | capital_gain | cash_sweep (income);
    # fee | deposit | withdrawal (cash_flow); core_cash (none)
    kind: str
    symbol: str | None = None
    description: str | None = None  # Fidelity's fund name, kept for an unpriced symbol
    quantity: Decimal | None = None
    price: Decimal | None = None
    fees: Decimal = Decimal(0)
    amount: Decimal  # positive dollars; kind carries the direction
    note: str
    already_imported: bool = False


class FeeLine(BaseModel):
    on: date
    amount: Decimal  # positive: what the account paid
    already_imported: bool = False


class PositionRow(BaseModel):
    """One row of a Fidelity Positions export."""

    account: str | None
    symbol: str
    description: str | None
    quantity: Decimal | None
    last_price: Decimal | None
    current_value: Decimal | None
    core: bool  # 'SPAXX**' and friends: cash, valued, no quantity


class PositionsFile(BaseModel):
    rows: list[PositionRow]
    pending_activity: Decimal = Decimal(0)
    downloaded_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cash(self) -> Decimal:
        """Core money market plus pending activity: what trd counts as cash, which
        includes trades booked but not yet settled."""
        core = sum((r.current_value or Decimal(0) for r in self.rows if r.core), Decimal(0))
        return core + self.pending_activity

    def quantities(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for r in self.rows:
            if not r.core and r.quantity is not None:
                out[r.symbol] = out.get(r.symbol, Decimal(0)) + r.quantity
        return out


class PositionCheckRow(BaseModel):
    symbol: str
    trd: Decimal
    fidelity: Decimal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def diff(self) -> Decimal:
        return self.trd - self.fidelity


class PositionsCheck(BaseModel):
    """trd's holdings and cash against a Positions export."""

    rows: list[PositionCheckRow]
    mismatched: list[str]
    cash_trd: Decimal
    cash_fidelity: Decimal
    cash_ok: bool
    projected: bool  # True on --dry-run: trd's side is the DB plus the planned writes
    # The Positions parser was written against Fidelity's documented layout
    # before a real export was available. Carried in the output until it has
    # been checked against one, so nobody mistakes a pass for a proven parser.
    note: str = (
        "Positions parser not yet verified against a real Fidelity Positions export; "
        "check the rows above against the file."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ok(self) -> bool:
        return not self.mismatched and self.cash_ok


class ImportPlan(BaseModel):
    account: str
    display_name: str | None = None
    create_account: bool = False
    dry_run: bool = True
    applied: bool = False
    first_date: date | None
    last_date: date | None
    rows: int
    writes: list[PlannedWrite]
    fees: list[FeeLine]
    # Fidelity's own running Cash Balance column, replayed: rows where the
    # balance does not follow from the previous row and this row's amount.
    # Zero means the parser and Fidelity agree about every dollar of cash.
    cash_balance_breaks: int = 0
    opening_cash: Decimal | None = None  # cash before the first row, per that column
    positions: PositionsCheck | None = None
    warnings: list[str] = []
    # Why this cannot be written (sells of shares never bought). The real run
    # raises it; a dry run carries it so the plan and its fees still print.
    refused: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def fees_total(self) -> Decimal:
        return sum((f.amount for f in self.fees), Decimal(0))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def new_writes(self) -> int:
        return sum(1 for w in self.writes if not w.already_imported)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def already_imported(self) -> int:
        return sum(1 for w in self.writes if w.already_imported)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def counts(self) -> dict[str, int]:
        """New writes by kind — what this run adds."""
        out: dict[str, int] = {}
        for w in self.writes:
            if not w.already_imported:
                out[w.kind] = out.get(w.kind, 0) + 1
        return dict(sorted(out.items()))
