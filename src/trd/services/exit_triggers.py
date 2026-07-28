from decimal import Decimal

import duckdb

from trd.errors import TrdError
from trd.models import Account, ExitCheckRow, ExitStatus, ExitTrigger
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, ExitTriggerRepo, InstrumentRepo, PriceRepo


class ExitTriggerService:
    """Stop/target exit rules on holdings. Evaluated against the latest stored daily
    close (run `trd sync` to refresh), so the read is deterministic and testable —
    no network in `rows`/`check`."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)
        self.triggers = ExitTriggerRepo(conn)

    def _account_or_fail(self, name: str) -> Account:
        account = self.accounts.get_by_name(name)
        if account is None:
            raise TrdError(f"No account named '{name}'.")
        return account

    def set(
        self,
        symbol: str,
        account_name: str,
        stop: Decimal | None = None,
        target: Decimal | None = None,
        note: str | None = None,
    ) -> ExitTrigger:
        """Create or replace a (account, instrument) exit trigger."""
        if stop is None and target is None:
            raise TrdError("Set at least one of --stop or --target.")
        if stop is not None and stop <= 0:
            raise TrdError("Stop price must be positive.")
        if target is not None and target <= 0:
            raise TrdError("Target price must be positive.")
        if stop is not None and target is not None and stop >= target:
            raise TrdError(f"Stop ({stop}) must be below target ({target}).")
        account = self._account_or_fail(account_name)
        instrument = self.instruments.get_by_symbol(symbol)
        if instrument is None:
            instrument = self.instruments.insert(self.provider.get_info(symbol))
        return self.triggers.upsert(account.id, instrument.id, stop, target, note)

    def remove(self, symbol: str, account_name: str) -> None:
        account = self._account_or_fail(account_name)
        instrument = self.instruments.get_by_symbol(symbol)
        if instrument is None or not self.triggers.remove(account.id, instrument.id):
            raise TrdError(f"No exit trigger for {symbol.upper()} in '{account_name}'.")

    def rows(
        self, account_name: str | None = None, breaches_only: bool = False
    ) -> list[ExitCheckRow]:
        """Every trigger evaluated against its latest stored close. With breaches_only,
        drop the calm (status OK) rows and keep only what needs attention."""
        account_id = None
        if account_name is not None:
            account_id = self._account_or_fail(account_name).id
        out: list[ExitCheckRow] = []
        for trigger, instrument, acct_name in self.triggers.list_all(account_id):
            row = ExitCheckRow(
                instrument=instrument,
                account=acct_name,
                stop_price=trigger.stop_price,
                target_price=trigger.target_price,
                note=trigger.note,
                last_close=self.prices.latest_close(instrument.id),
            )
            if breaches_only and row.status == ExitStatus.OK:
                continue
            out.append(row)
        return out
