import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from trd.errors import InsufficientPositionError, TrdError, UnknownAccountError
from trd.models import AccountType, Instrument, LotPosition, Position, Side, Transaction
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo, TransactionRepo
from trd.services.fifo import fifo_position, open_lots

CSV_COLUMNS = ["date", "account", "symbol", "side", "quantity", "price", "fees", "note"]


class PortfolioService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.instruments = InstrumentRepo(conn)
        self.accounts = AccountRepo(conn)
        self.txns = TransactionRepo(conn)
        self.prices = PriceRepo(conn)

    def ensure_instrument(self, symbol: str) -> Instrument:
        """Find a tracked instrument, or resolve it via the provider and track it."""
        existing = self.instruments.get_by_symbol(symbol)
        if existing:
            return existing
        info = self.provider.get_info(symbol)
        return self.instruments.insert(info)

    def record_trade(
        self,
        account_name: str,
        symbol: str,
        side: Side,
        quantity: Decimal,
        price: Decimal | None = None,
        fees: Decimal = Decimal(0),
        executed_at: datetime | None = None,
        note: str | None = None,
        plan_id: int | None = None,
    ) -> Transaction:
        account = self.accounts.get_by_name(account_name)
        if account is None:
            raise UnknownAccountError(account_name)
        instrument = self.ensure_instrument(symbol)
        if price is None:
            price = self.provider.get_quote(instrument.symbol).price
        if side == Side.SELL:
            held = self._held_quantity(account.id, instrument.id)
            if quantity > held:
                raise InsufficientPositionError(
                    instrument.symbol, f"{held.normalize():f}", f"{quantity.normalize():f}"
                )
        return self.txns.insert(
            account_id=account.id,
            instrument_id=instrument.id,
            side=side,
            quantity=quantity,
            price=price,
            fees=fees,
            executed_at=executed_at or datetime.now(),
            note=note,
            plan_id=plan_id,
        )

    def _held_quantity(self, account_id: int, instrument_id: int) -> Decimal:
        txns = [
            t for t in self.txns.list_chronological(account_id) if t.instrument_id == instrument_id
        ]
        quantity, _ = fifo_position(txns)
        return quantity

    def _scope_txns(
        self, account_name: str | None, include_simulation: bool
    ) -> dict[tuple[int, int], list[Transaction]]:
        """Transactions grouped per (account, instrument). FIFO must run per pair:
        a sell in one account can never consume lots held in another.

        The all-accounts view excludes simulation (paper) accounts unless
        include_simulation; naming an account explicitly always includes it.
        """
        account_id: int | None = None
        if account_name is not None:
            account = self.accounts.get_by_name(account_name)
            if account is None:
                raise UnknownAccountError(account_name)
            account_id = account.id

        skip: set[int] = set()
        if account_id is None and not include_simulation:
            skip = {a.id for a in self.accounts.list_all() if a.type == AccountType.SIMULATION}

        by_pair: dict[tuple[int, int], list[Transaction]] = defaultdict(list)
        for txn in self.txns.list_chronological(account_id):
            if txn.account_id in skip:
                continue
            by_pair[(txn.account_id, txn.instrument_id)].append(txn)
        return by_pair

    def positions(
        self, account_name: str | None = None, include_simulation: bool = False
    ) -> list[Position]:
        """Open positions with FIFO cost basis and current prices.

        Live quotes are fetched per call (and snapshotted); a symbol whose quote
        fails falls back to its latest stored snapshot, marked stale.
        """
        by_pair = self._scope_txns(account_name, include_simulation)

        totals: dict[int, tuple[Decimal, Decimal]] = defaultdict(lambda: (Decimal(0), Decimal(0)))
        for (_, instrument_id), txns in by_pair.items():
            quantity, cost_basis = fifo_position(txns)
            q, c = totals[instrument_id]
            totals[instrument_id] = (q + quantity, c + cost_basis)

        open_positions: list[tuple[Instrument, Decimal, Decimal]] = []
        for instrument_id, (quantity, cost_basis) in totals.items():
            if quantity == 0:
                continue
            instrument = self.instruments.get(instrument_id)
            assert instrument is not None
            open_positions.append((instrument, quantity, cost_basis))

        symbols = [inst.symbol for inst, _, _ in open_positions]
        quotes = self.provider.get_quotes(symbols)

        positions: list[Position] = []
        for instrument, quantity, cost_basis in open_positions:
            quote = quotes.get(instrument.symbol)
            if quote is not None:
                self.prices.insert_snapshot(instrument.id, quote.price, quote.prev_close)
                price, prev_close, stale = quote.price, quote.prev_close, False
            else:
                snapshot = self.prices.latest_snapshot(instrument.id)
                if snapshot is not None:
                    price, prev_close, stale = snapshot[0], snapshot[1], True
                else:
                    price, prev_close, stale = None, None, True
            positions.append(
                Position(
                    instrument=instrument,
                    quantity=quantity,
                    cost_basis=cost_basis,
                    price=price,
                    prev_close=prev_close,
                    price_stale=stale,
                )
            )
        positions.sort(key=lambda p: p.instrument.symbol)
        return positions

    def lots(
        self,
        account_name: str | None = None,
        symbol: str | None = None,
        include_simulation: bool = False,
    ) -> list[LotPosition]:
        """Surviving buy lots with live prices: buy date, paid/share, cost, gain.

        Same quote strategy as positions(): live fetch, snapshot fallback marked stale.
        """
        by_pair = self._scope_txns(account_name, include_simulation)

        account_names = {a.id: a.name for a in self.accounts.list_all()}
        selected: list[tuple[str, Instrument, list]] = []
        for (txn_account_id, instrument_id), txns in by_pair.items():
            instrument = self.instruments.get(instrument_id)
            assert instrument is not None
            if symbol is not None and instrument.symbol != symbol.upper():
                continue
            surviving = open_lots(txns)
            if surviving:
                selected.append((account_names.get(txn_account_id, "?"), instrument, surviving))

        quotes = self.provider.get_quotes(list({inst.symbol for _, inst, _ in selected}))

        result: list[LotPosition] = []
        for lot_account, instrument, surviving in selected:
            quote = quotes.get(instrument.symbol)
            if quote is not None:
                self.prices.insert_snapshot(instrument.id, quote.price, quote.prev_close)
                price, stale = quote.price, False
            else:
                snapshot = self.prices.latest_snapshot(instrument.id)
                price, stale = (snapshot[0], True) if snapshot else (None, True)
            for lot in surviving:
                result.append(
                    LotPosition(
                        instrument=instrument,
                        account=lot_account,
                        bought_at=lot.bought_at,
                        quantity=lot.quantity,
                        price_paid=lot.price,
                        cost=lot.cost,
                        price=price,
                        price_stale=stale,
                    )
                )
        result.sort(key=lambda lot: (lot.instrument.symbol, lot.bought_at, lot.account))
        return result

    def sparklines(self, positions: list[Position], days: int = 30) -> dict[str, list[float]]:
        """Recent daily closes per held symbol, for inline trend sparklines."""
        out: dict[str, list[float]] = {}
        for position in positions:
            closes = self.prices.recent_closes(position.instrument.id, days)
            if len(closes) >= 2:
                out[position.instrument.symbol] = [float(c) for c in closes]
        return out

    def import_csv(self, path: Path) -> int:
        """Bulk-load transactions. Columns: date,account,symbol,side,quantity,price[,fees,note]."""
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise TrdError(f"{path}: empty file")
            missing = {"date", "account", "symbol", "side", "quantity", "price"} - set(
                reader.fieldnames
            )
            if missing:
                raise TrdError(f"{path}: missing columns {sorted(missing)}")
            count = 0
            for line, row in enumerate(reader, start=2):
                try:
                    self.record_trade(
                        account_name=row["account"].strip(),
                        symbol=row["symbol"].strip(),
                        side=Side(row["side"].strip().lower()),
                        quantity=Decimal(row["quantity"]),
                        price=Decimal(row["price"]),
                        fees=Decimal(row.get("fees") or "0"),
                        executed_at=datetime.fromisoformat(row["date"].strip()),
                        note=(row.get("note") or "").strip() or None,
                    )
                except (ValueError, KeyError, ArithmeticError) as exc:
                    raise TrdError(f"{path}:{line}: bad row ({exc})") from exc
                count += 1
        return count
