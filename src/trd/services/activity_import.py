"""Import a managed account's own CSV exports (SB-1418).

Not `services/replay_match.py`, which is the engine's replay-match check and
has nothing to do with the brokerage.

The custodian offers no retail API, so a custodian-managed account — one an adviser
trades, for a fee — is tracked from the exports its website gives you:

- **Activity History** (required): every buy, sell, distribution, reinvestment
  and fee, newest first. It carries no account number, so `--account` is always
  stated, never inferred.
- **Positions** (optional): what is held now. Used to reconcile — every symbol's
  quantity and the cash — and, on a first import, to back out what was held
  before the activity starts (`opening_from_positions`).

How each row lands, and why:

- YOU BOUGHT / REINVESTMENT -> a `txn` buy; YOU SOLD -> a `txn` sell. The price is
  derived from the Amount (cash that actually moved) rather than read from the
  Price column, which is rounded to cents: over a year of fund trades the
  rounding is real money, and the cash ledger has to land on the custodian's.
- DIVIDEND RECEIVED -> income `dividend`; LONG-/SHORT-TERM CAP GAIN -> income
  `capital_gain`. A reinvested distribution is TWO rows in the export — the
  cash arriving, then the cash buying shares — and is recorded as exactly those
  two facts, once each: income in, a buy out. Cash nets to zero, the shares are
  a real lot, and nothing is counted twice.
- The core money market (SPAXX and its kin) IS the account's cash. Its dividend
  is interest on cash (income `cash_sweep`, no instrument); its reinvestment,
  buys and sells are cash staying cash and write nothing.
- ADVISORY FEE -> cash_flow `fee`. Money out of the account that is a COST, so
  it lowers every return (`services/cashflow.py`); never a withdrawal, which a
  return excuses. Every import lists each fee and the total.
- CONTRIBUTION / ROLLOVER / TRANSFER / JOURNALED / ELECTRONIC FUNDS TRANSFER ->
  cash_flow deposit or withdrawal, by the Amount's sign.
- Anything else refuses the whole import with every offending line listed.
  Nothing is guessed and nothing is skipped quietly.

Idempotent: each row is keyed (run date | action class | symbol | quantity |
amount | occurrence among identical keys in the file) in `import_row`, so the
same file, or an overlapping later one, writes each row once. Assumes an export
covers whole days, which the custodian's date-range downloads do.

Opening state (built: `opening_from_positions`): an activity history does not
start at zero. With a Positions export taken AFTER the activity, what was held
before the first row is Positions minus the activity's net quantity per symbol;
the cash before it is read off the activity's own running Cash Balance column.
Those are seeded the day before the first row — buys at that day's provider
close (or `opening_prices`), note "opening balance from the positions export",
and one deposit of their value plus the cash — so the account's return is
measured from that day and not from nothing. Only on an account with no history.

Parsing is pure (`parse_activity`, `parse_positions`, `plan_rows`) and needs no
database.
"""

import csv
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path

import duckdb

from trd.errors import ProviderError, SymbolNotFoundError, TrdError, UnknownAccountError
from trd.models import AccountType, IncomeKind, InstrumentInfo, InstrumentType, Side
from trd.models.activity_import import (
    ActivityKind,
    ActivityRow,
    FeeLine,
    ImportPlan,
    PlannedWrite,
    PositionCheckRow,
    PositionRow,
    PositionsCheck,
    PositionsFile,
    WriteTarget,
)
from trd.models.alloc import CashFlowKind
from trd.models.reconcile import CASH_TOLERANCE
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, TransactionRepo
from trd.repos.alloc import CashFlowRepo
from trd.repos.income import IncomeRepo
from trd.services.alloc import CashService
from trd.services.portfolio import PortfolioService
from trd.services.reconcile import ReconcileService

SOURCE = "activity"
CENT = Decimal("0.01")
PRICE_PLACES = Decimal("0.00000001")  # txn.price is DECIMAL(24, 8)
# Shares either side may differ by before a symbol is a mismatch: The custodian
# states fund quantities to three decimals.
POSITION_TOLERANCE = Decimal("0.001")
# the custodian's core money-market funds: the account's cash, never a holding. A
# Positions export marks the core with a trailing '**' as well.
CORE_SYMBOLS = frozenset({"SPAXX", "FDRXX", "FZFXX", "SPRXX", "FCASH", "CORE"})
OPENING_NOTE = "opening balance from the positions export"

ACTIVITY_COLUMNS = (
    "Run Date",
    "Action",
    "Symbol",
    "Description",
    "Price ($)",
    "Quantity",
    "Commission ($)",
    "Fees ($)",
    "Amount ($)",
)

# Matched against the START of the Action text, upper-cased with whitespace
# collapsed. The rest is the fund name, sometimes preceded by "PROSPECTUS UNDER
# SEPARATE COVER" or "as of YYYY-MM-DD".
_ACTION_PREFIXES: tuple[tuple[str, ActivityKind], ...] = (
    ("YOU BOUGHT", ActivityKind.BUY),
    ("YOU SOLD", ActivityKind.SELL),
    ("REINVESTMENT", ActivityKind.REINVESTMENT),
    ("DIVIDEND RECEIVED", ActivityKind.DIVIDEND),
    ("LONG-TERM CAP GAIN", ActivityKind.CAPITAL_GAIN_LONG),
    ("SHORT-TERM CAP GAIN", ActivityKind.CAPITAL_GAIN_SHORT),
    ("ADVISORY FEE", ActivityKind.ADVISORY_FEE),
    ("CONTRIBUTION", ActivityKind.TRANSFER),
    ("ROLLOVER", ActivityKind.TRANSFER),
    ("TRANSFER", ActivityKind.TRANSFER),
    ("JOURNALED", ActivityKind.TRANSFER),
    ("ELECTRONIC FUNDS TRANSFER", ActivityKind.TRANSFER),
)

_NEEDS_SYMBOL = {
    ActivityKind.BUY,
    ActivityKind.SELL,
    ActivityKind.REINVESTMENT,
    ActivityKind.DIVIDEND,
    ActivityKind.CAPITAL_GAIN_LONG,
    ActivityKind.CAPITAL_GAIN_SHORT,
}
# Rows on the core money market that move no cash: buying the core with cash is
# still cash.
_CORE_NO_CASH = {ActivityKind.BUY, ActivityKind.SELL, ActivityKind.REINVESTMENT}


def classify_action(action: str) -> ActivityKind | None:
    text = " ".join(action.split()).upper()
    for prefix, kind in _ACTION_PREFIXES:
        if text.startswith(prefix):
            return kind
    return None


def _is_core(symbol: str | None) -> bool:
    return symbol is not None and symbol.rstrip("*").upper() in CORE_SYMBOLS


def _money(raw: str | None) -> Decimal | None:
    """'$1,234.50', '+2.1', '(3.00)', '-12' -> Decimal; '', '--', 'n/a' -> None."""
    if raw is None:
        return None
    text = raw.strip().replace("$", "").replace(",", "").replace("+", "").replace("%", "")
    if text in ("", "--", "-", "n/a", "N/A"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {raw!r}") from exc
    return -value if negative else value


def _us_date(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%m/%d/%Y").date()


def _num(value: Decimal) -> str:
    """A stable spelling for a key: '-12.50' and '-12.5' are the same row."""
    return f"{value.normalize():f}" if value != 0 else "0"


def _lines(text: str) -> list[str]:
    return text.lstrip("﻿").splitlines()


def _header_index(lines: Sequence[str], first_cell: str | tuple[str, ...]) -> int | None:
    wanted = (first_cell,) if isinstance(first_cell, str) else first_cell
    for i, line in enumerate(lines):
        cells = next(csv.reader([line]), [])
        if cells and cells[0].strip() in wanted:
            return i
    return None


def _data_lines(lines: Sequence[str], header: int) -> list[tuple[int, str]]:
    """(1-based line number, text) from below the header up to the first blank
    line. Everything after that blank is the custodian's legal footer."""
    out: list[tuple[int, str]] = []
    for i in range(header + 1, len(lines)):
        if not lines[i].strip():
            break
        out.append((i + 1, lines[i]))
    return out


def parse_activity(text: str) -> list[ActivityRow]:
    """An Activity History export -> rows in file order (newest first).

    Refuses the whole file if any row is an action this does not know, or one
    whose signs contradict its action — listing every such line, not the first.
    """
    lines = _lines(text)
    header = _header_index(lines, "Run Date")
    if header is None:
        raise TrdError("Not an Activity History export: no 'Run Date,Action,...' header found.")
    columns = [c.strip() for c in next(csv.reader([lines[header]]))]
    missing = [c for c in ACTIVITY_COLUMNS if c not in columns]
    if missing:
        raise TrdError(f"Activity export is missing columns: {', '.join(missing)}.")

    rows: list[ActivityRow] = []
    problems: list[str] = []
    for line_no, line in _data_lines(lines, header):
        cells = next(csv.reader([line]))
        raw = dict(zip(columns, (c.strip() for c in cells), strict=False))
        action = " ".join(raw.get("Action", "").split())
        kind = classify_action(action)
        if kind is None:
            problems.append(f"line {line_no}: unknown action {action!r}")
            continue
        try:
            row = _activity_row(line_no, raw, action, kind)
        except ValueError as exc:
            problems.append(f"line {line_no}: {exc}")
            continue
        problem = _sign_problem(row)
        if problem:
            problems.append(f"line {line_no}: {problem}")
            continue
        rows.append(row)
    if problems:
        raise TrdError(
            "Refusing the whole import — these rows are not understood, and guessing at "
            "money is worse than stopping:\n  " + "\n  ".join(problems)
        )
    return rows


def _activity_row(line: int, raw: dict[str, str], action: str, kind: ActivityKind) -> ActivityRow:
    try:
        run_date = _us_date(raw["Run Date"])
    except ValueError as exc:
        raise ValueError(f"bad Run Date {raw.get('Run Date')!r}") from exc
    amount = _money(raw.get("Amount ($)"))
    if amount is None:
        raise ValueError("no Amount")
    settle = raw.get("Settlement Date", "")
    symbol = raw.get("Symbol") or None
    return ActivityRow(
        line=line,
        run_date=run_date,
        action=action,
        kind=kind,
        symbol=symbol.upper() if symbol else None,
        description=raw.get("Description") or None,
        price=_money(raw.get("Price ($)")),
        quantity=_money(raw.get("Quantity")) or Decimal(0),
        fees=(_money(raw.get("Commission ($)")) or Decimal(0))
        + (_money(raw.get("Fees ($)")) or Decimal(0)),
        amount=amount,
        cash_balance=_money(raw.get("Cash Balance ($)")),
        settlement_date=_us_date(settle) if settle else None,
        core=_is_core(symbol),
    )


def _sign_problem(row: ActivityRow) -> str | None:
    k, q, a = row.kind, row.quantity, row.amount
    if k in _NEEDS_SYMBOL and row.symbol is None:
        return f"{row.action!r} has no symbol"
    if k in (ActivityKind.BUY, ActivityKind.REINVESTMENT) and not (q > 0 and a < 0):
        return f"a buy needs a positive quantity and negative amount (got {q}, {a})"
    if k == ActivityKind.SELL and not (q < 0 and a > 0):
        return f"a sell needs a negative quantity and positive amount (got {q}, {a})"
    if k in (
        ActivityKind.DIVIDEND,
        ActivityKind.CAPITAL_GAIN_LONG,
        ActivityKind.CAPITAL_GAIN_SHORT,
    ) and not (a > 0):
        return f"a distribution needs a positive amount (got {a})"
    if k == ActivityKind.ADVISORY_FEE and not (a < 0):
        return f"an advisory fee needs a negative amount (got {a}); a refund is not handled"
    if k == ActivityKind.TRANSFER and a == 0:
        return "a transfer of zero"
    return None


def cash_effect(row: ActivityRow) -> Decimal:
    """What the row did to the account's cash. A core money-market buy, sell or
    reinvestment is cash staying cash."""
    if row.core and row.kind in _CORE_NO_CASH:
        return Decimal(0)
    return row.amount


def balance_check(rows: Sequence[ActivityRow]) -> tuple[int, Decimal | None]:
    """Replay the custodian's running Cash Balance column (rows newest first).

    Returns how many rows break the chain — this row's balance is not the older
    row's balance plus this row's cash effect — and the cash before the oldest
    row. Zero breaks means this parser and the custodian agree on every dollar.
    """
    breaks = 0
    for newer, older in pairwise(rows):
        if newer.cash_balance is None or older.cash_balance is None:
            continue
        if newer.cash_balance != older.cash_balance + cash_effect(newer):
            breaks += 1
    opening = None
    if rows and rows[-1].cash_balance is not None:
        opening = rows[-1].cash_balance - cash_effect(rows[-1])
    return breaks, opening


def _trade_price(row: ActivityRow, quantity: Decimal) -> Decimal:
    """Price per share from the cash that moved: buy |amount| = q*p + fees,
    sell amount = q*p - fees."""
    gross = -row.amount - row.fees if row.amount < 0 else row.amount + row.fees
    return (gross / quantity).quantize(PRICE_PLACES, rounding=ROUND_HALF_UP)


def _write_for(row: ActivityRow, key: str) -> PlannedWrite:
    def make(
        target: WriteTarget,
        kind: str,
        amount: Decimal,
        note: str,
        symbol: str | None = row.symbol,
        quantity: Decimal | None = None,
        price: Decimal | None = None,
        fees: Decimal = Decimal(0),
    ) -> PlannedWrite:
        return PlannedWrite(
            key=key,
            line=row.line,
            on=row.run_date,
            target=target,
            kind=kind,
            symbol=symbol,
            description=row.description,
            quantity=quantity,
            price=price,
            fees=fees,
            amount=amount,
            note=note,
        )

    k = row.kind
    if row.core and k in _CORE_NO_CASH:
        return make(
            WriteTarget.NONE,
            "core_cash",
            abs(row.amount),
            f"core money market {k.value}: cash stays cash, nothing written",
        )
    if k in (ActivityKind.BUY, ActivityKind.REINVESTMENT, ActivityKind.SELL):
        quantity = abs(row.quantity)
        side = Side.SELL if k == ActivityKind.SELL else Side.BUY
        return make(
            WriteTarget.TXN,
            side.value,
            abs(row.amount),
            "import: dividend reinvested"
            if k == ActivityKind.REINVESTMENT
            else f"import: {side.value}",
            quantity=quantity,
            price=_trade_price(row, quantity),
            fees=row.fees,
        )
    if k == ActivityKind.DIVIDEND:
        if row.core:
            return make(
                WriteTarget.INCOME,
                IncomeKind.CASH_SWEEP.value,
                row.amount,
                f"import: core money market interest ({row.symbol})",
                symbol=None,
            )
        return make(WriteTarget.INCOME, IncomeKind.DIVIDEND.value, row.amount, "import: dividend")
    if k in (ActivityKind.CAPITAL_GAIN_LONG, ActivityKind.CAPITAL_GAIN_SHORT):
        term = "long-term" if k == ActivityKind.CAPITAL_GAIN_LONG else "short-term"
        return make(
            WriteTarget.INCOME,
            IncomeKind.CAPITAL_GAIN.value,
            row.amount,
            f"import: {term} capital gain distribution",
        )
    if k == ActivityKind.ADVISORY_FEE:
        return make(
            WriteTarget.CASH_FLOW,
            CashFlowKind.FEE.value,
            -row.amount,
            "advisory fee",
            symbol=None,
        )
    flow = CashFlowKind.DEPOSIT if row.amount > 0 else CashFlowKind.WITHDRAWAL
    return make(
        WriteTarget.CASH_FLOW, flow.value, abs(row.amount), f"import: {row.action}", symbol=None
    )


def row_keys(rows_oldest_first: Iterable[ActivityRow]) -> list[str]:
    """One idempotency key per row. Identical rows in one file get 0, 1, 2 ... so
    a genuine duplicate imports, and imports once."""
    seen: dict[str, int] = {}
    keys: list[str] = []
    for row in rows_oldest_first:
        base = "|".join(
            (
                row.run_date.isoformat(),
                row.kind.value,
                row.symbol or "-",
                _num(row.quantity),
                _num(row.amount),
            )
        )
        n = seen.get(base, 0)
        seen[base] = n + 1
        keys.append(f"{base}|{n}")
    return keys


def plan_rows(rows: Sequence[ActivityRow]) -> list[PlannedWrite]:
    """Rows (file order) -> writes, oldest first: the order FIFO needs them in."""
    oldest_first = list(reversed(rows))
    return [
        _write_for(row, key) for row, key in zip(oldest_first, row_keys(oldest_first), strict=True)
    ]


def write_cash(write: PlannedWrite) -> Decimal:
    """What a planned write does to trd's cash for the account."""
    if write.target == WriteTarget.TXN:
        assert write.quantity is not None and write.price is not None
        gross = write.quantity * write.price
        return -(gross + write.fees) if write.kind == Side.BUY.value else gross - write.fees
    if write.target == WriteTarget.INCOME:
        return write.amount
    if write.target == WriteTarget.CASH_FLOW:
        return write.amount if write.kind == CashFlowKind.DEPOSIT.value else -write.amount
    return Decimal(0)


# --------------------------------------------------------------- positions
#
# UNTESTED AGAINST A REAL FILE. Written to the custodian's documented Positions
# layout (Account Number, Account Name, Symbol, Description, Quantity, Last
# Price, ..., Current Value, ...; the core as 'SPAXX**' with a value and no
# quantity; a 'Pending Activity' row) before a real export was supplied.
# Columns are read by NAME so a reordering is harmless; check the parsed rows
# against the first real export before trusting a reconciliation.

_POSITIONS_FIRST_CELLS = ("Account Number", "Account Name", "Symbol")
_DOWNLOADED = re.compile(r"Date downloaded\s+(.+)$", re.IGNORECASE)
_DOWNLOADED_FORMATS = ("%m/%d/%Y %I:%M %p", "%b-%d-%Y %I:%M %p", "%m/%d/%Y")


def _downloaded_at(lines: Sequence[str]) -> datetime | None:
    for line in reversed(lines):
        match = _DOWNLOADED.search(line.strip().strip('"'))
        if not match:
            continue
        text = match.group(1).replace("a.m.", "am").replace("p.m.", "pm").replace(" ET", "")
        text = text.strip().strip('"')
        for fmt in _DOWNLOADED_FORMATS:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def parse_positions(text: str, account: str | None = None) -> PositionsFile:
    """A Positions export -> rows for ONE the custodian account.

    A login with several accounts downloads them all into one file; `account`
    (an Account Number or Account Name) picks one. Several and none named is
    refused rather than summed.
    """
    lines = _lines(text)
    header = _header_index(lines, _POSITIONS_FIRST_CELLS)
    if header is None:
        raise TrdError("Not a Positions export: no 'Account Number,...' header found.")
    columns = [c.strip() for c in next(csv.reader([lines[header]]))]
    for needed in ("Symbol", "Quantity", "Current Value"):
        if needed not in columns:
            raise TrdError(f"Positions export is missing the '{needed}' column.")

    parsed: list[tuple[str | None, str | None, PositionRow | Decimal]] = []
    for line_no, line in _data_lines(lines, header):
        raw = dict(zip(columns, (c.strip() for c in next(csv.reader([line]))), strict=False))
        number, name = raw.get("Account Number") or None, raw.get("Account Name") or None
        symbol = (raw.get("Symbol") or "").strip()
        try:
            value = _money(raw.get("Current Value"))
            if symbol.lower().startswith("pending activity"):
                parsed.append((number, name, value or Decimal(0)))
                continue
            if not symbol:
                continue
            parsed.append(
                (
                    number,
                    name,
                    PositionRow(
                        account=number or name,
                        symbol=symbol.rstrip("*").upper(),
                        description=raw.get("Description") or None,
                        quantity=_money(raw.get("Quantity")),
                        last_price=_money(raw.get("Last Price")),
                        current_value=value,
                        core=symbol.endswith("**") or _is_core(symbol),
                    ),
                )
            )
        except ValueError as exc:
            raise TrdError(f"Positions line {line_no}: {exc}") from exc

    accounts = sorted({n or a for n, a, _ in parsed if (n or a)})
    if account is not None:
        wanted = account.strip().lower()
        parsed = [
            p for p in parsed if (p[0] or "").lower() == wanted or (p[1] or "").lower() == wanted
        ]
        if not parsed:
            raise TrdError(
                f"No rows for account {account!r} in the Positions export "
                f"(it has: {', '.join(accounts) or 'none named'})."
            )
    elif len(accounts) > 1:
        raise TrdError(
            "The Positions export holds several accounts "
            f"({', '.join(accounts)}); name one with --source-account."
        )
    rows = [p[2] for p in parsed if isinstance(p[2], PositionRow)]
    pending = sum((p[2] for p in parsed if isinstance(p[2], Decimal)), Decimal(0))
    return PositionsFile(rows=rows, pending_activity=pending, downloaded_at=_downloaded_at(lines))


def check_positions(
    held: dict[str, Decimal], cash: Decimal, positions: PositionsFile, projected: bool
) -> PositionsCheck:
    """trd's quantities and cash against the custodian's, symbol by symbol."""
    theirs = positions.quantities()
    rows = [
        PositionCheckRow(symbol=s, trd=held.get(s, Decimal(0)), broker=theirs.get(s, Decimal(0)))
        for s in sorted(set(held) | set(theirs))
    ]
    rows = [r for r in rows if r.trd != 0 or r.broker != 0]
    return PositionsCheck(
        rows=rows,
        mismatched=[r.symbol for r in rows if abs(r.diff) > POSITION_TOLERANCE],
        cash_trd=cash,
        cash_broker=positions.cash,
        cash_ok=abs(cash - positions.cash) <= CASH_TOLERANCE,
        projected=projected,
    )


# ----------------------------------------------------------------- service


@dataclass
class _Context:
    rows: list[ActivityRow]
    positions: PositionsFile | None
    account_id: int | None


class ActivityImportService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.accounts = AccountRepo(conn)
        self.txns = TransactionRepo(conn)
        self.income = IncomeRepo(conn)
        self.flows = CashFlowRepo(conn)
        self.cash = CashService(conn)
        self.portfolio = PortfolioService(conn, provider)
        self.reconciler = ReconcileService(conn)

    def run(
        self,
        activity: Path,
        account: str,
        *,
        positions: Path | None = None,
        create: str | None = None,
        dry_run: bool = False,
        opening_from_positions: bool = False,
        opening_prices: dict[str, Decimal] | None = None,
        source_account: str | None = None,
    ) -> ImportPlan:
        """Plan the import and, unless `dry_run`, write it in one transaction."""
        plan, ctx = self._plan(
            activity.read_text(encoding="utf-8-sig"),
            account,
            positions_text=positions.read_text(encoding="utf-8-sig") if positions else None,
            create=create,
            opening_from_positions=opening_from_positions,
            opening_prices=opening_prices or {},
            source_account=source_account,
        )
        plan.dry_run = dry_run
        if dry_run:
            return plan  # a refusal is in plan.refused, so the fees still print
        if plan.refused:
            raise TrdError(plan.refused)
        self._apply(plan)
        plan.applied = True
        if ctx.positions is not None:
            plan.positions = check_positions(
                self.reconciler.held(account),
                self.cash.balance(account).cash,
                ctx.positions,
                projected=False,
            )
        return plan

    # ------------------------------------------------------------ planning

    def _plan(
        self,
        activity_text: str,
        account: str,
        *,
        positions_text: str | None,
        create: str | None,
        opening_from_positions: bool,
        opening_prices: dict[str, Decimal],
        source_account: str | None,
    ) -> tuple[ImportPlan, _Context]:
        rows = parse_activity(activity_text)
        positions = parse_positions(positions_text, source_account) if positions_text else None
        existing = self.accounts.get_by_name(account)
        if existing is None and create is None:
            raise UnknownAccountError(account)
        if existing is not None and existing.type != AccountType.REAL:
            raise TrdError(
                f"'{account}' is a {existing.type.value} account; an activity export is real money."
            )
        account_id = existing.id if existing else None

        writes = plan_rows(rows)
        done = self._imported_keys(account_id)
        for w in writes:
            w.already_imported = w.key in done
        breaks, opening_cash = balance_check(rows)
        warnings: list[str] = []
        if breaks:
            warnings.append(
                f"{breaks} row(s) do not follow the custodian's running Cash Balance — the "
                "cash trd derives may differ from the custodian's."
            )

        if opening_from_positions:
            if positions is None:
                raise TrdError("--opening-from-positions needs --positions.")
            if rows:
                opening = self._opening(
                    account, account_id, rows, writes, positions, opening_cash, opening_prices
                )
                for w in opening:
                    w.already_imported = w.key in done
                writes = opening + writes
        downloaded = positions.downloaded_at if positions else None
        if downloaded is not None and rows and downloaded.date() < rows[0].run_date:
            warnings.append(
                "The Positions export was downloaded before the last activity row; "
                "it cannot reflect it."
            )

        held = self.reconciler.held(account) if account_id else {}

        fees = [
            FeeLine(on=w.on, amount=w.amount, already_imported=w.already_imported)
            for w in writes
            if w.kind == CashFlowKind.FEE.value
        ]
        plan = ImportPlan(
            account=account,
            display_name=create,
            create_account=existing is None,
            first_date=rows[-1].run_date if rows else None,
            last_date=rows[0].run_date if rows else None,
            rows=len(rows),
            writes=writes,
            fees=fees,
            cash_balance_breaks=breaks,
            opening_cash=opening_cash,
            warnings=warnings,
            refused=self._oversold(held, writes),
        )
        if positions is not None:
            projected = dict(held)
            cash = self.cash.balance(account).cash if account_id else Decimal(0)
            for w in writes:
                if w.already_imported:
                    continue
                cash += write_cash(w)
                if w.target == WriteTarget.TXN and w.symbol and w.quantity is not None:
                    sign = 1 if w.kind == Side.BUY.value else -1
                    projected[w.symbol] = projected.get(w.symbol, Decimal(0)) + sign * w.quantity
            plan.positions = check_positions(
                {s: q for s, q in projected.items() if q != 0}, cash, positions, projected=True
            )
        return plan, _Context(rows=rows, positions=positions, account_id=account_id)

    def _imported_keys(self, account_id: int | None) -> set[str]:
        if account_id is None:
            return set()
        return {
            r[0]
            for r in self.conn.execute(
                "SELECT row_key FROM import_row WHERE account_id = ? AND source = ?",
                [account_id, SOURCE],
            ).fetchall()
        }

    def _opening(
        self,
        account: str,
        account_id: int | None,
        rows: Sequence[ActivityRow],
        writes: Sequence[PlannedWrite],
        positions: PositionsFile,
        opening_cash: Decimal | None,
        opening_prices: dict[str, Decimal],
    ) -> list[PlannedWrite]:
        """What was held the day before the first row: Positions minus the activity."""
        if account_id is not None and self._has_history(account_id):
            if "opening|deposit" in self._imported_keys(account_id):
                return []  # seeded by an earlier run of this same import
            raise TrdError(
                f"'{account}' already has history; an opening balance is only seeded on a "
                "first import. Drop --opening-from-positions."
            )
        net: dict[str, Decimal] = {}
        cash_moved = Decimal(0)
        descriptions: dict[str, str | None] = {}
        for w in writes:
            cash_moved += write_cash(w)
            if w.target == WriteTarget.TXN and w.symbol and w.quantity is not None:
                sign = 1 if w.kind == Side.BUY.value else -1
                net[w.symbol] = net.get(w.symbol, Decimal(0)) + sign * w.quantity
                descriptions.setdefault(w.symbol, w.description)
        held_now = positions.quantities()
        for r in positions.rows:
            descriptions.setdefault(r.symbol, r.description)

        opening: dict[str, Decimal] = {}
        impossible: list[str] = []
        for symbol in sorted(set(net) | set(held_now)):
            q = held_now.get(symbol, Decimal(0)) - net.get(symbol, Decimal(0))
            if abs(q) <= POSITION_TOLERANCE:
                continue
            if q < 0:
                impossible.append(f"{symbol}: activity sold {-q} more than Positions explains")
                continue
            opening[symbol] = q
        if impossible:
            raise TrdError(
                "Positions and activity disagree — an opening holding would be negative:\n  "
                + "\n  ".join(impossible)
            )

        on = rows[-1].run_date - timedelta(days=1)
        prices: dict[str, Decimal] = {}
        unpriced: list[str] = []
        for symbol in opening:
            price = opening_prices.get(symbol) or self._close_on_or_before(symbol, on)
            if price is None:
                unpriced.append(symbol)
            else:
                prices[symbol] = price
        if unpriced:
            raise TrdError(
                f"No close on or before {on} for {', '.join(unpriced)} — the opening lots "
                "need a price. Pass --opening-price SYMBOL=PRICE for each."
            )
        if opening_cash is None:
            opening_cash = positions.cash - cash_moved

        out = [
            PlannedWrite(
                key=f"opening|{symbol}",
                line=None,
                on=on,
                target=WriteTarget.TXN,
                kind=Side.BUY.value,
                symbol=symbol,
                description=descriptions.get(symbol),
                quantity=q,
                price=prices[symbol],
                amount=(q * prices[symbol]).quantize(CENT, rounding=ROUND_HALF_UP),
                note=OPENING_NOTE,
            )
            for symbol, q in opening.items()
        ]
        holdings = sum((q * prices[s] for s, q in opening.items()), Decimal(0))
        total = (holdings + opening_cash).quantize(CENT, rounding=ROUND_HALF_UP)
        if total <= 0:
            raise TrdError(f"Opening balance would be {total}; nothing to seed.")
        out.insert(
            0,
            PlannedWrite(
                key="opening|deposit",
                line=None,
                on=on,
                target=WriteTarget.CASH_FLOW,
                kind=CashFlowKind.DEPOSIT.value,
                amount=total,
                note=f"{OPENING_NOTE}: holdings {holdings.quantize(CENT)} + cash "
                f"{opening_cash.quantize(CENT)}",
            ),
        )
        return out

    def _has_history(self, account_id: int) -> bool:
        return bool(
            self.txns.list_chronological(account_id)
            or self.income.list_all(account_id)
            or self.flows.list_all(account_id)
        )

    def _close_on_or_before(self, symbol: str, on: date) -> Decimal | None:
        try:
            bars = self.provider.get_daily_bars(
                symbol, on - timedelta(days=10), on + timedelta(days=1)
            )
        except ProviderError:
            return None
        eligible = [b for b in bars if b.date <= on]
        return max(eligible, key=lambda b: b.date).close if eligible else None

    @staticmethod
    def _oversold(held: dict[str, Decimal], writes: Sequence[PlannedWrite]) -> str | None:
        """Why the import cannot be written, or None: a sell of shares trd never
        saw bought. Refused on the real run; reported on a dry run."""
        running = dict(held)
        short: list[str] = []
        for w in writes:
            if w.already_imported or w.target != WriteTarget.TXN or not w.symbol:
                continue
            assert w.quantity is not None
            have = running.get(w.symbol, Decimal(0))
            if w.kind == Side.SELL.value:
                if w.quantity > have:
                    where = f"line {w.line}" if w.line else "opening"
                    short.append(f"{where} {w.on} {w.symbol}: sells {w.quantity}, holds {have}")
                running[w.symbol] = have - w.quantity
            else:
                running[w.symbol] = have + w.quantity
        if not short:
            return None
        return (
            "The activity sells shares trd has no record of — the account held them "
            "before the export starts:\n  "
            + "\n  ".join(short)
            + "\nPass --positions POSITIONS.csv --opening-from-positions to seed what was "
            "held before the first row."
        )

    # ------------------------------------------------------------- writing

    def _apply(self, plan: ImportPlan) -> None:
        self.conn.begin()
        try:
            account = self.accounts.get_by_name(plan.account)
            if account is None:
                account = self.accounts.create(plan.account, AccountType.REAL)
            if plan.display_name is not None:
                self.conn.execute(
                    """INSERT INTO import_account (account_id, source, display_name, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT (account_id)
                       DO UPDATE SET display_name = excluded.display_name""",
                    [account.id, SOURCE, plan.display_name, datetime.now()],
                )
            now = datetime.now()
            for w in plan.writes:
                if w.already_imported:
                    continue
                self._write(account.id, plan.account, w)
                self.conn.execute(
                    "INSERT INTO import_row (account_id, source, row_key, imported_at) "
                    "VALUES (?, ?, ?, ?)",
                    [account.id, SOURCE, w.key, now],
                )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _write(self, account_id: int, account: str, w: PlannedWrite) -> None:
        # Opening lots sit at the close of the day before the first row; a row's
        # writes at midnight of its run date, in file order (FIFO's tie-break is id).
        when = (
            datetime.combine(w.on, time(16, 0))
            if w.line is None
            else datetime.combine(w.on, time())
        )
        if w.target == WriteTarget.TXN:
            assert w.symbol and w.quantity is not None and w.price is not None
            self._instrument(w.symbol, w.description)
            self.portfolio.record_trade(
                account,
                w.symbol,
                Side(w.kind),
                w.quantity,
                price=w.price,
                fees=w.fees,
                executed_at=when,
                note=w.note,
            )
        elif w.target == WriteTarget.INCOME:
            instrument_id = self._instrument(w.symbol, w.description).id if w.symbol else None
            self.income.add(
                account_id=account_id,
                amount=w.amount,
                received_at=when,
                kind=IncomeKind(w.kind),
                instrument_id=instrument_id,
                note=w.note,
            )
        elif w.target == WriteTarget.CASH_FLOW:
            self.flows.add(account_id, CashFlowKind(w.kind), w.amount.quantize(CENT), when, w.note)

    def _instrument(self, symbol: str, description: str | None):
        """The provider's instrument, or — for a fund it does not list (many
        adviser-only share classes) — one named from the custodian's description. It
        is then simply unpriced: a missing price reads unknown, never zero."""
        try:
            return self.portfolio.ensure_instrument(symbol)
        except SymbolNotFoundError:
            return self.portfolio.instruments.insert(
                InstrumentInfo(symbol=symbol, name=description, type=InstrumentType.ETF)
            )
