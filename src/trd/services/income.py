"""Recording the cash a holding paid you.

Thin on purpose: resolving an account and a symbol to ids, validating an
amount, and handing the row to the repo. Everything that *reasons* about income
lives in `cashflow`, where the three return calculations share one definition.
"""

from datetime import datetime
from decimal import Decimal

import duckdb

from trd.errors import TrdError, UnknownAccountError
from trd.models import Income, IncomeKind, Instrument
from trd.providers.base import MarketDataProvider
from trd.repos import AccountRepo, InstrumentRepo
from trd.repos.income import IncomeRepo
from trd.services.portfolio import PortfolioService


class IncomeService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.income = IncomeRepo(conn)
        # The one place a symbol becomes an instrument. A second copy here would
        # be a second set of rules about what trd is willing to track.
        self.portfolio = PortfolioService(conn, provider)

    def add(
        self,
        account_name: str,
        amount: Decimal,
        received_at: datetime,
        symbol: str | None = None,
        kind: IncomeKind = IncomeKind.DIVIDEND,
        note: str | None = None,
    ) -> Income:
        """Record one payment.

        A negative or zero amount is refused. Income is cash arriving; a
        negative one would flow through XIRR as a contribution and quietly
        improve the return it was meant to correct.
        """
        if amount <= 0:
            raise TrdError(
                f"Income must be positive, got {amount}. This records cash received — "
                "a fee or a withdrawal is not negative income."
            )
        account = self.accounts.get_by_name(account_name)
        if account is None:
            raise UnknownAccountError(account_name)

        instrument_id = None
        if symbol is not None:
            instrument_id = self.portfolio.ensure_instrument(symbol.upper()).id
        elif kind is IncomeKind.DIVIDEND:
            # A dividend is paid BY something. Letting one through unattached
            # would make it invisible to a plan's share of it, and unexplainable
            # in a year's time.
            raise TrdError(
                "A dividend needs a symbol — it is paid by a holding. Use "
                "--kind interest or --kind cash_sweep for cash paid by the account itself."
            )
        return self.income.add(
            account_id=account.id,
            amount=amount,
            received_at=received_at,
            kind=kind,
            instrument_id=instrument_id,
            note=note,
        )

    def ledger(self, account_name: str | None = None) -> list[tuple[Income, Instrument | None]]:
        """Every payment with the holding that paid it, oldest first."""
        account_id = None
        if account_name is not None:
            account = self.accounts.get_by_name(account_name)
            if account is None:
                raise UnknownAccountError(account_name)
            account_id = account.id
        return [
            (i, self.instruments.get(i.instrument_id) if i.instrument_id else None)
            for i in self.income.list_all(account_id)
        ]
