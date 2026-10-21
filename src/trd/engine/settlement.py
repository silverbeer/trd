"""Settled cash: what a cash account may actually spend today.

In a cash account a sale's proceeds settle T+1 — one business day after the
trade — and a broker excludes them from buying power until then. Robinhood does.
The engine had no cash concept at all: capacity was a slot count, and a scan runs
its exits first and refills the freed slot in the same pass. Replayed on the live
swing paper book since 2026-07-12, 105 of 112 entries came the same day as an
earlier exit and 86 (77%) would have needed unsettled proceeds. A broker rejects
those orders, so a live engine and its paper twin would trade different books
after the very first exit.

Pure arithmetic over (date, side, quantity, price, fees), so the live scan, paper
and the backtest can build the same ledger and skip the same entries. Off unless
the engine has a capital (migration 026).
"""

from collections.abc import Iterable
from datetime import date, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, computed_field

from trd.models import Side

# One flow of cash through the account: (trade date, side, quantity, price, fees).
Flow = tuple[date, Side, Decimal, Decimal, Decimal]


def settles_on(trade_date: date) -> date:
    """The day a trade's cash settles: the next weekday (T+1).

    There is no holiday calendar in trd, deliberately (see SESSION_OPEN_MINUTE in
    trd.timeframes), so around a market holiday this is one day optimistic: a
    Wednesday sale before a Thursday holiday settles Friday, and this says
    Thursday. The live path cross-checks the broker's own buying power, which is
    authoritative; this ledger exists so paper and the backtest refuse the same
    same-day refills the broker would.
    """
    day = trade_date + timedelta(days=1)
    while day.weekday() >= 5:  # Saturday, Sunday
        day += timedelta(days=1)
    return day


class CashLedger(BaseModel):
    """An engine account's cash as of one day.

    `settled` is what may be spent now. `unsettled` is sale proceeds still waiting
    to settle, broken out by settle date in `pending`, oldest first. Their sum
    with `settled` is the account's cash; only `settled` is buying power.
    """

    model_config = ConfigDict(frozen=True)

    capital: Decimal
    settled: Decimal
    unsettled: Decimal
    pending: list[tuple[date, Decimal]] = []

    # computed_field, not a plain property: it is part of `engine cash --json`.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_settle(self) -> tuple[date, Decimal] | None:
        """The earliest proceeds still waiting, and how much of them there is."""
        return self.pending[0] if self.pending else None

    def spend(self, amount: Decimal) -> "CashLedger":
        """The ledger after a buy in this same pass — settled cash only, because
        unsettled proceeds are exactly what a cash account may not spend."""
        return self.model_copy(update={"settled": self.settled - amount})

    def shortfall(self, symbol: str, cost: Decimal) -> str:
        """Why an entry costing `cost` was refused, naming when cash arrives.

        A refusal that does not say when it lifts reads as a broken engine; the
        settle date is what tells a reader this is the T+1 rule and it ends.
        """
        head = f"{symbol}: ${cost:,.2f} needed, ${self.settled:,.2f} settled"
        nxt = self.next_settle
        if nxt is None:
            return f"{head} — nothing pending settlement (cash account)"
        when, amount = nxt
        return f"{head} — ${amount:,.2f} from earlier exits settles {when} (T+1, cash account)"


def ledger(capital: Decimal, flows: Iterable[Flow], today: date) -> CashLedger:
    """The ledger as of `today`, from a starting capital and every trade since.

    A buy costs `quantity x price + fees` on its trade date — the cash leaves at
    once. A sell brings in `quantity x price - fees`, settled once
    `settles_on(trade date) <= today` and pending until then. A partial exit is
    just a sell. Flows dated after `today` are not part of an as-of view and are
    ignored.
    """
    settled = capital
    pending: dict[date, Decimal] = {}
    for trade_date, side, quantity, price, fees in flows:
        if trade_date > today:
            continue
        if side == Side.BUY:
            settled -= quantity * price + fees
            continue
        proceeds = quantity * price - fees
        settle = settles_on(trade_date)
        if settle <= today:
            settled += proceeds
        else:
            pending[settle] = pending.get(settle, Decimal(0)) + proceeds
    ordered = sorted(pending.items())
    return CashLedger(
        capital=capital,
        settled=settled,
        unsettled=sum((amount for _, amount in ordered), Decimal(0)),
        pending=ordered,
    )


# Sub-cent slack when asking whether a buy fit inside settled cash: fractional
# fills round, and a buy that overshoots settled cash by a fraction of a cent was
# not funded with anybody's unsettled proceeds.
FUNDING_TOLERANCE = Decimal("0.01")


def unsettled_funding(
    capital: Decimal, prior: Iterable[Flow], trade_date: date, cost: Decimal
) -> date | None:
    """When the unsettled cash a buy spent settles, or None if settled cash paid
    for all of it.

    `prior` is every flow before the buy, in order. The good-faith-violation test
    hangs off this: a cash account that sells a position before the cash which
    bought it has settled has sold something not yet paid for. The ledger refuses
    such buys in the first place (an entry needs settled cash), so for any book
    the engine built this returns None — it exists so the live exit path can say
    so loudly if that ever stops being true.
    """
    before = ledger(capital, prior, trade_date)
    short = cost - before.settled
    if short <= FUNDING_TOLERANCE:
        return None
    covered = Decimal(0)
    for when, amount in before.pending:
        covered += amount
        if covered >= short - FUNDING_TOLERANCE:
            return when
    # Spent more than settled and pending together: funded by nothing trd knows
    # about. The latest pending date is the earliest it could have been paid for.
    return before.pending[-1][0] if before.pending else trade_date
