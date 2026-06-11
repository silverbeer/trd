import csv
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb

from trd.errors import InsufficientPositionError, TrdError, UnknownAccountError
from trd.models import Instrument, Position, Side, Transaction
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, InstrumentRepo, PriceRepo, TransactionRepo
from trd.services.fifo import fifo_position

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
        )

    def _held_quantity(self, account_id: int, instrument_id: int) -> Decimal:
        txns = [
            t for t in self.txns.list_chronological(account_id) if t.instrument_id == instrument_id
        ]
        quantity, _ = fifo_position(txns)
        return quantity

    def positions(self, account_name: str | None = None) -> list[Position]:
        """Open positions with FIFO cost basis and current prices.

        Live quotes are fetched per call (and snapshotted); a symbol whose quote
        fails falls back to its latest stored snapshot, marked stale.
        """
        account_id: int | None = None
        if account_name is not None:
            account = self.accounts.get_by_name(account_name)
            if account is None:
                raise UnknownAccountError(account_name)
            account_id = account.id

        by_instrument: dict[int, list[Transaction]] = defaultdict(list)
        for txn in self.txns.list_chronological(account_id):
            by_instrument[txn.instrument_id].append(txn)

        open_positions: list[tuple[Instrument, Decimal, Decimal]] = []
        for instrument_id, txns in by_instrument.items():
            quantity, cost_basis = fifo_position(txns)
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
