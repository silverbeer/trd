"""Diff a broker snapshot against what trd believes it holds.

Pure arithmetic over a snapshot file and the database — no network, no provider.
The brokerage read happens elsewhere (an authenticated MCP session; see
docs/broker-mcp.md) precisely so this part stays deterministic and testable
without a broker account.

trd's side is FIFO-derived from transactions, never a stored balance, so this
compares the broker against the same number every other trd command shows. That
matters: reconciling against a cached balance would only prove the cache agrees
with itself.
"""

from collections import defaultdict
from decimal import Decimal

import duckdb

from trd.engine.settlement import CashLedger
from trd.errors import TrdError, UnknownAccountError
from trd.models import (
    QUANTITY_TOLERANCE,
    BrokerSnapshot,
    CashCheck,
    ReconcileRow,
    ReconcileStatus,
    Reconciliation,
)
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo, TransactionRepo
from trd.services.fifo import fifo_position


class ReconcileService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.txns = TransactionRepo(conn)
        self.prices = PriceRepo(conn)

    def held(self, account_name: str) -> dict[str, Decimal]:
        """Symbol -> quantity for one account, FIFO over its whole history.

        Zero-quantity symbols are dropped: a fully exited position is not a
        holding, and listing it would report a mismatch against every broker
        that quite correctly never mentions it.
        """
        account = self.accounts.get_by_name(account_name)
        if account is None:
            raise UnknownAccountError(account_name)

        by_instrument: dict[int, list] = defaultdict(list)
        for txn in self.txns.list_chronological(account.id):
            by_instrument[txn.instrument_id].append(txn)

        out: dict[str, Decimal] = {}
        for instrument_id, txns in by_instrument.items():
            quantity, _ = fifo_position(txns)
            if quantity == 0:
                continue
            instrument = self.instruments.get(instrument_id)
            if instrument is not None:
                out[instrument.symbol] = quantity
        return out

    def reconcile(
        self,
        snapshot: BrokerSnapshot,
        account_name: str,
        cash: CashLedger | None = None,
    ) -> Reconciliation:
        """One row per symbol either side holds, sorted with the problems first.

        Ordering is deliberate: this command exists to be scanned for trouble,
        and a matched book scrolls a screen of `ok` past whatever is wrong.

        `cash` is the engine's settled-cash ledger, passed only when the caller
        has been told the broker account's cash is the engine's alone
        (`--compare-cash`). The broker routinely holds more than an engine's
        capital, so the comparison is opt-in rather than a mismatch reported
        every day. Given a
        ledger, the snapshot must carry `cash`; a check with nothing to compare
        would read as a pass.
        """
        check = None
        if cash is not None:
            if snapshot.cash is None:
                raise TrdError(
                    "--compare-cash needs the broker's cash in the snapshot: add 'cash' "
                    "(settled buying power) and, if known, 'unsettled'."
                )
            check = CashCheck(
                trd_settled=cash.settled,
                trd_unsettled=cash.unsettled,
                broker_settled=snapshot.cash,
                broker_unsettled=snapshot.unsettled,
            )
        trd_held = self.held(account_name)
        broker_held = {p.symbol: p for p in snapshot.positions}

        rows: list[ReconcileRow] = []
        for symbol in sorted(set(trd_held) | set(broker_held)):
            broker = broker_held.get(symbol)
            broker_quantity = broker.quantity if broker is not None else None
            trd_quantity = trd_held.get(symbol)

            if broker_quantity is None:
                status = ReconcileStatus.MISSING_AT_BROKER
            elif trd_quantity is None:
                status = ReconcileStatus.UNTRACKED
            elif abs(broker_quantity - trd_quantity) <= QUANTITY_TOLERANCE:
                status = ReconcileStatus.OK
            else:
                status = ReconcileStatus.QUANTITY

            close_date, close = None, None
            instrument = self.instruments.get_by_symbol(symbol)
            if instrument is not None:
                dated = self.prices.latest_close_dated(instrument.id)
                if dated is not None:
                    close_date, close = dated

            rows.append(
                ReconcileRow(
                    symbol=symbol,
                    broker_quantity=broker_quantity,
                    trd_quantity=trd_quantity,
                    broker_price=broker.price if broker is not None else None,
                    trd_price=close,
                    trd_price_date=close_date,
                    status=status,
                )
            )

        rows.sort(key=lambda r: (r.matched, r.symbol))
        return Reconciliation(
            account=account_name,
            as_of=snapshot.as_of,
            source=snapshot.source,
            broker_cash=snapshot.cash,
            broker_unsettled=snapshot.unsettled,
            rows=rows,
            cash=check,
        )
