"""Monthly head-to-head: retirement accounts against each other and the market,
after fees (SB-1420).

Every account is valued the same way, from what trd holds for it — FIFO
quantities at the day's close, plus the cash its own records imply — so a
managed account imported from the custodian's exports and a self-managed one
are measured by one rule.

Returns are Modified Dietz over a window: (V1 - V0 - F) / (V0 + sum(w * f)),
with F the money the owner put in or took out and w the share of the window
each flow was there for. Not annualized, so a month reads as a month. An
advisory fee is NOT a flow: it is money the account lost, so it lowers the
return. "Before fees" is the same arithmetic with the fees handed back as if
they had been withdrawals — the difference is exactly what management cost.

Benchmarks are "the same dollars, the same days": the account's starting value
bought into the index at the window's first close, and every deposit bought on
the day it arrived, then scored with the same formula. Priced on ADJUSTED closes
(dividends reinvested), because the accounts reinvest theirs; an unadjusted
index would hand every account a free ~1.5% a year.

Windows end at a settled close, never "now": the provider's newest close for a
fund can lag the custodian by a day (2026-11-17: managed-only funds repeated the
previous close), and a month-end close a day later is final.
"""

import calendar
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import duckdb

from trd.errors import TrdError, UnknownAccountError
from trd.models import Transaction
from trd.models.alloc import CashFlow, CashFlowKind
from trd.models.core import Income
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo
from trd.repos.alloc import CashFlowRepo
from trd.repos.income import IncomeRepo
from trd.repos.instruments import InstrumentRepo
from trd.repos.transactions import TransactionRepo
from trd.services.alloc_report import Closes
from trd.services.fifo import fifo_position

SP500 = "SPY"
BOND = "AGG"
# (key, label, weights). 60/40 is a real mix of two legs, each bought at its own
# close — not a balanced fund standing in for one.
BENCHMARKS: list[tuple[str, str, dict[str, Decimal]]] = [
    ("sp500", "S&P 500", {SP500: Decimal(1)}),
    ("sixty_forty", "60/40", {SP500: Decimal("0.6"), BOND: Decimal("0.4")}),
]
FETCH_PAD_DAYS = 10
HUNDRED = Decimal(100)


# ------------------------------------------------------------------ the maths


def month_window(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def dietz(
    v0: Decimal, v1: Decimal, flows: Sequence[tuple[date, Decimal]], start: date, end: date
) -> Decimal | None:
    """Modified Dietz return in percent over [start, end]. `v0` is the value at
    the close BEFORE `start`; a flow on day d is in for (end - d + 1) of the
    window's days. None when there was no capital to measure."""
    days = (end - start).days + 1
    inside = [(on, a) for on, a in flows if start <= on <= end]
    net = sum((a for _, a in inside), Decimal(0))
    weighted = sum((a * Decimal((end - on).days + 1) / days for on, a in inside), Decimal(0))
    capital = v0 + weighted
    if capital <= 0:
        return None
    return (v1 - v0 - net) / capital * HUNDRED


def cash_on(
    on: date, flows: Iterable[CashFlow], income: Iterable[Income], txns: Iterable[Transaction]
) -> Decimal:
    """The account's cash at the close of `on`: deposits less withdrawals and
    fees, plus every payment received, less what trades cost and plus what they
    raised."""
    cash = sum((f.signed for f in flows if f.occurred_at.date() <= on), Decimal(0))
    cash += sum((i.amount for i in income if i.received_at.date() <= on), Decimal(0))
    for t in txns:
        if t.executed_at.date() > on:
            continue
        gross = t.quantity * t.price
        cash += -(gross + t.fees) if t.side == "buy" else gross - t.fees
    return cash


def held_on(
    on: date, txns: Sequence[Transaction], symbols: dict[int, str], closes: dict[str, Closes]
) -> Decimal | None:
    """Holdings at the close of `on`. None if any held position has no close —
    a partial sum would read as the whole account."""
    by_instrument: dict[int, list[Transaction]] = {}
    for t in txns:
        if t.executed_at.date() <= on:
            by_instrument.setdefault(t.instrument_id, []).append(t)
    total = Decimal(0)
    for instrument_id, rows in by_instrument.items():
        quantity, _ = fifo_position(rows)
        if quantity == 0:
            continue
        series = closes.get(symbols[instrument_id])
        close = series.on_or_before(on) if series is not None else None
        if close is None:
            return None
        total += quantity * close
    return total


def basket_value(
    v0: Decimal,
    flows: Sequence[tuple[date, Decimal]],
    weights: dict[str, Decimal],
    closes: dict[str, Closes],
    start: date,
    end: date,
) -> Decimal | None:
    """`v0` bought at the close before `start`, each flow inside the window on
    its day (a withdrawal sells), valued at `end`'s close."""
    total = Decimal(0)
    for symbol, weight in weights.items():
        series = closes.get(symbol)
        if series is None:
            return None
        shares = Decimal(0)
        buys = [(start - timedelta(days=1), v0)] if v0 else []
        buys += [(on, a) for on, a in flows if start <= on <= end]
        for on, amount in buys:
            price = series.on_or_before(on)
            if price is None or price == 0:
                return None
            shares += amount * weight / price
        last = series.on_or_before(end)
        if last is None:
            return None
        total += shares * last
    return total


# ------------------------------------------------------------------ the report


@dataclass
class Fees:
    month: Decimal
    ytd: Decimal
    trailing_12m: Decimal
    annual_rate_pct: Decimal | None  # trailing 12m fees / average month-end value


@dataclass
class WindowResult:
    start: date
    end: date
    v0: Decimal | None
    v1: Decimal | None
    net_flows: Decimal
    fees: Decimal
    return_pct: Decimal | None  # after fees
    before_fees_pct: Decimal | None
    benchmarks: dict[str, Decimal | None] = field(default_factory=dict)  # key -> return %


@dataclass
class AccountResult:
    label: str
    managed: bool  # fed by custodian imports
    imported_through: date | None  # newest import run; None for a self-managed account
    fresh: bool
    month: WindowResult
    since: WindowResult
    fees: Fees | None


@dataclass
class CompareReport:
    month_start: date
    month_end: date
    partial: bool  # the month is not over: valued through `month_end`
    since: date
    accounts: list[AccountResult]
    benchmark_labels: dict[str, str]
    leaders_month: list[str]  # ties share the lead; empty = no verdict
    leaders_since: list[str]


@dataclass
class _Data:
    txns: list[Transaction]
    flows: list[CashFlow]
    income: list[Income]
    imported_at: date | None


class IraCompareService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)

    def report(
        self,
        accounts: Sequence[tuple[str, str]],
        year: int,
        month: int,
        since: date,
        today: date | None = None,
    ) -> CompareReport:
        """`accounts` is (trd account name, label). Labels, never names, leave
        this service: account names are private and reports reach Telegram."""
        today = today or date.today()
        month_start, month_end = month_window(year, month)
        settled = today - timedelta(days=1)
        if month_start > settled:
            raise TrdError(f"{year}-{month:02d} has no settled close yet.")
        partial = month_end > settled
        month_end = min(month_end, settled)
        if since > month_end:
            raise TrdError(f"--since {since} is after the window ends ({month_end}).")

        data: dict[str, _Data] = {name: self._load(name) for name, _ in accounts}
        symbols = {
            i: self._symbol(i) for d in data.values() for i in {t.instrument_id for t in d.txns}
        }
        wanted = set(symbols.values()) | {s for _, _, w in BENCHMARKS for s in w}
        earliest = min(since, month_start, date(month_end.year, 1, 1), _year_back(month_end))
        closes, adjusted = self._closes(
            wanted, earliest - timedelta(days=FETCH_PAD_DAYS), month_end
        )

        results: list[AccountResult] = []
        for name, label in accounts:
            d = data[name]
            managed = d.imported_at is not None
            fresh = not managed or (d.imported_at is not None and d.imported_at > month_end)
            month_r = self._window(d, symbols, closes, adjusted, month_start, month_end)
            since_r = self._window(d, symbols, closes, adjusted, since, month_end)
            fees = self._fees(d, symbols, closes, month_start, month_end) if managed else None
            results.append(
                AccountResult(label, managed, d.imported_at, fresh, month_r, since_r, fees)
            )
        return CompareReport(
            month_start=month_start,
            month_end=month_end,
            partial=partial,
            since=since,
            accounts=results,
            benchmark_labels={k: label for k, label, _ in BENCHMARKS},
            leaders_month=leaders(results, lambda a: a.month.return_pct),
            leaders_since=leaders(results, lambda a: a.since.return_pct),
        )

    def common_start(self, names: Sequence[str]) -> date:
        """The first day every account had money: the latest of their first
        deposits. Before it there is nothing to compare one account with."""
        firsts: list[date] = []
        for name in names:
            d = self._load(name)
            dates = [f.occurred_at.date() for f in d.flows if f.external]
            dates += [t.executed_at.date() for t in d.txns]
            if not dates:
                raise TrdError(f"Account '{name}' holds nothing to compare.")
            firsts.append(min(dates))
        return max(firsts)

    # -------------------------------------------------------------- pieces

    def _load(self, name: str) -> _Data:
        account = self.accounts.get_by_name(name)
        if account is None:
            raise UnknownAccountError(name)
        row = self.conn.execute(
            "SELECT max(imported_at) FROM import_row WHERE account_id = ?", [account.id]
        ).fetchone()
        imported = row[0].date() if row and row[0] is not None else None
        return _Data(
            txns=TransactionRepo(self.conn).list_chronological(account.id),
            flows=CashFlowRepo(self.conn).list_all(account.id),
            income=IncomeRepo(self.conn).list_all(account.id),
            imported_at=imported,
        )

    def _symbol(self, instrument_id: int) -> str:
        instrument = self.instruments.get(instrument_id)
        assert instrument is not None
        return instrument.symbol

    def _closes(
        self, symbols: Iterable[str], start: date, end: date
    ) -> tuple[dict[str, Closes], dict[str, Closes]]:
        """(raw, adjusted) provider closes. Read-only: nothing stored.

        Accounts are valued on RAW closes — their dividends are already in them,
        as cash or reinvested shares. Benchmarks use ADJUSTED closes, the index
        with its dividends reinvested. Mixing them counts a dividend twice."""
        raw: dict[str, Closes] = {}
        adjusted: dict[str, Closes] = {}
        for symbol in symbols:
            try:
                bars = self.provider.get_daily_bars(symbol, start, end + timedelta(days=1))
            except TrdError:
                bars = []
            raw[symbol] = Closes({b.date: b.close for b in bars})
            adjusted[symbol] = Closes(
                {b.date: b.adj_close if b.adj_close is not None else b.close for b in bars}
            )
        return raw, adjusted

    def _value(
        self, d: _Data, symbols: dict[int, str], closes: dict[str, Closes], on: date
    ) -> Decimal | None:
        held = held_on(on, d.txns, symbols, closes)
        return None if held is None else held + cash_on(on, d.flows, d.income, d.txns)

    def _window(
        self,
        d: _Data,
        symbols: dict[int, str],
        closes: dict[str, Closes],
        adjusted: dict[str, Closes],
        start: date,
        end: date,
    ) -> WindowResult:
        v0 = self._value(d, symbols, closes, start - timedelta(days=1))
        v1 = self._value(d, symbols, closes, end)
        flows = [(f.occurred_at.date(), f.signed) for f in d.flows if f.external]
        fee_rows = [
            (f.occurred_at.date(), f.amount)
            for f in d.flows
            if f.kind == CashFlowKind.FEE and start <= f.occurred_at.date() <= end
        ]
        fees = sum((a for _, a in fee_rows), Decimal(0))
        net = sum((a for on, a in flows if start <= on <= end), Decimal(0))
        result = WindowResult(start, end, v0, v1, net, fees, None, None)
        if v0 is None or v1 is None:
            return result
        result.return_pct = dietz(v0, v1, flows, start, end)
        gross_flows = flows + [(on, -a) for on, a in fee_rows]
        result.before_fees_pct = dietz(v0, v1, gross_flows, start, end)
        for key, _, weights in BENCHMARKS:
            bench_v1 = basket_value(v0, flows, weights, adjusted, start, end)
            result.benchmarks[key] = (
                None if bench_v1 is None else dietz(v0, bench_v1, flows, start, end)
            )
        return result

    def _fees(
        self,
        d: _Data,
        symbols: dict[int, str],
        closes: dict[str, Closes],
        month_start: date,
        month_end: date,
    ) -> Fees:
        def total(start: date) -> Decimal:
            return sum(
                (
                    f.amount
                    for f in d.flows
                    if f.kind == CashFlowKind.FEE and start <= f.occurred_at.date() <= month_end
                ),
                Decimal(0),
            )

        year_back = _year_back(month_end)
        values = [
            v
            for on in _month_ends(year_back, month_end)
            if (v := self._value(d, symbols, closes, on)) is not None and v > 0
        ]
        trailing = total(year_back)
        average = sum(values, Decimal(0)) / len(values) if values else Decimal(0)
        rate = trailing / average * HUNDRED if average and trailing else None
        return Fees(
            month=total(month_start),
            ytd=total(date(month_end.year, 1, 1)),
            trailing_12m=trailing,
            annual_rate_pct=rate,
        )


def _year_back(end: date) -> date:
    return end - timedelta(days=364)


def _month_ends(start: date, end: date) -> list[date]:
    """Each month end from `start` through `end`, and `end` itself."""
    out: list[date] = []
    y, m = start.year, start.month
    while True:
        last = month_window(y, m)[1]
        if last >= end:
            break
        if last >= start:
            out.append(last)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    out.append(end)
    return out


# Returns closer than this are a tie: the message shows one decimal, and a
# "winner" by a hundredth of a point is noise in the prices, not a verdict.
TIE_PTS = Decimal("0.05")


def leaders(
    results: list[AccountResult], pick: Callable[[AccountResult], Decimal | None]
) -> list[str]:
    """The fresh account(s) with the best after-fee return. Empty when fewer than
    two fresh accounts are scored, or any fresh one is unknown — a verdict over
    a partial field would be a guess."""
    fresh = [a for a in results if a.fresh]
    scored = [(r, a.label) for a in fresh if (r := pick(a)) is not None]
    if len(scored) < 2 or len(scored) < len(fresh):
        return []
    best = max(r for r, _ in scored)
    return [label for r, label in scored if best - r < TIE_PTS]
