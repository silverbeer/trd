"""Transaction history: what was bought and sold, and whether it made money.

The `txn` table is the system of record — portfolio, FIFO lots, the equity curve
and XIRR are all derived from it — and until now it was the one thing with no way
to look at it. `trd lots` shows surviving buy lots, so a position that was sold
disappears entirely; `trd dca history` and `trd engine positions --all` each cover
one slice.

Every sell carries its realized result, matched against the lots it consumed. A
sell row without a result is half the story: the point of looking back at a trade
is finding out whether it worked.
"""

from datetime import date, timedelta
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError
from trd.models import Account, AccountType, Instrument, Side, Transaction
from trd.repos import AccountRepo, InstrumentRepo, TransactionRepo
from trd.services.fifo import RealizedSale, realized_sales


class HistoryRow(BaseModel):
    """One transaction, joined to what it was and where."""

    txn: Transaction
    instrument: Instrument
    account: str
    realized_pnl: Decimal | None = None  # sells only
    realized_pct: Decimal | None = None

    @property
    def gross(self) -> Decimal:
        """Cash moved: what a buy cost, or what a sell returned, fees included."""
        signed = self.txn.quantity * self.txn.price
        return signed + self.txn.fees if self.txn.side == Side.BUY else signed - self.txn.fees


class HistoryResult(BaseModel):
    rows: list[HistoryRow]
    since: date | None
    bought: Decimal  # cash out
    sold: Decimal  # cash in
    realized_pnl: Decimal  # from sells that settled in the window
    sells_with_result: int
    fees: Decimal

    @property
    def net_invested(self) -> Decimal:
        """Cash committed over the period. Positive means money went in."""
        return self.bought - self.sold

    @property
    def realized_pct(self) -> Decimal | None:
        cost = self.sold - self.realized_pnl
        if cost <= 0:
            return None
        return self.realized_pnl / cost * 100


class HistoryService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.txns = TransactionRepo(conn)

    def history(
        self,
        days: int | None = 30,
        account: str | None = None,
        symbol: str | None = None,
        side: Side | None = None,
        include_simulation: bool = False,
    ) -> HistoryResult:
        """Transactions in the window, newest first, with realized P&L on sells.

        Real accounts only unless `include_simulation`: the engine makes several
        paper fills a day, and left in they bury the handful of trades actually
        worth reviewing.
        """
        accounts = {a.id: a for a in self.accounts.list_all()}
        if account is not None:
            named = next((a for a in accounts.values() if a.name == account), None)
            if named is None:
                raise TrdError(f"No account named '{account}'.")
            wanted = {named.id}
        else:
            wanted = {
                a.id
                for a in accounts.values()
                if include_simulation or a.type != AccountType.SIMULATION
            }

        instruments = {i.id: i for i in self.instruments.list_all()}
        wanted_instrument = None
        if symbol is not None:
            found = self.instruments.get_by_symbol(symbol.upper())
            if found is None:
                raise TrdError(f"Unknown symbol '{symbol.upper()}'.")
            wanted_instrument = found.id

        # FIFO needs each instrument's whole history for an account, not just the
        # window — matching a sale against lots that were filtered out would
        # invent a profit. So: match over everything, display the window.
        everything = self.txns.list_chronological()
        grouped: dict[tuple[int, int], list[Transaction]] = {}
        for txn in everything:
            grouped.setdefault((txn.account_id, txn.instrument_id), []).append(txn)
        realized: dict[int, RealizedSale] = {}
        for series in grouped.values():
            realized.update(realized_sales(series))

        since = date.today() - timedelta(days=days) if days is not None else None
        rows: list[HistoryRow] = []
        for txn in everything:
            if txn.account_id not in wanted:
                continue
            if wanted_instrument is not None and txn.instrument_id != wanted_instrument:
                continue
            if side is not None and txn.side != side:
                continue
            if since is not None and txn.executed_at.date() < since:
                continue
            instrument = instruments.get(txn.instrument_id)
            if instrument is None:
                continue
            sale = realized.get(txn.id) if txn.side == Side.SELL else None
            rows.append(
                HistoryRow(
                    txn=txn,
                    instrument=instrument,
                    account=_account_name(accounts.get(txn.account_id)),
                    realized_pnl=sale.pnl if sale is not None else None,
                    realized_pct=sale.pnl_pct if sale is not None else None,
                )
            )
        rows.sort(key=lambda r: (r.txn.executed_at, r.txn.id), reverse=True)

        bought = sum((r.gross for r in rows if r.txn.side == Side.BUY), Decimal(0))
        sold = sum((r.gross for r in rows if r.txn.side == Side.SELL), Decimal(0))
        realized_total = sum(
            (r.realized_pnl for r in rows if r.realized_pnl is not None), Decimal(0)
        )
        return HistoryResult(
            rows=rows,
            since=since,
            bought=bought,
            sold=sold,
            realized_pnl=realized_total,
            sells_with_result=sum(1 for r in rows if r.realized_pnl is not None),
            fees=sum((r.txn.fees for r in rows), Decimal(0)),
        )


def _account_name(account: Account | None) -> str:
    return account.name if account is not None else "?"
