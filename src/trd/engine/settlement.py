"""Settled cash: what a cash account may actually spend today.

In a cash account a sale's proceeds settle T+1 — one business day after the
trade — and a broker excludes them from buying power until then. Some brokers do.
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

    `settled` is the engine's own settled cash. `unsettled` is sale proceeds still
    waiting to settle, broken out by settle date in `pending`, oldest first. Their
    sum is the engine's cash.

    `bridge` is spare settled cash the owner keeps in the broker account beyond
    the capital (SB-1445). It may front proceeds already on their way and nothing
    more, so `buying_power` is `settled + min(bridge, unsettled)`: the engine
    never spends money it has not made, only a day sooner. A buy the bridge pays
    for takes `settled` below zero; that deficit is `bridged`, and the proceeds
    settling the next business day pay it back. With no bridge, buying power is
    `settled` alone, exactly as before.
    """

    model_config = ConfigDict(frozen=True)

    capital: Decimal
    settled: Decimal
    unsettled: Decimal
    pending: list[tuple[date, Decimal]] = []
    bridge: Decimal = Decimal(0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def bridged(self) -> Decimal:
        """How much of the bridge is fronting unsettled proceeds right now."""
        return max(Decimal(0), -self.settled)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def buying_power(self) -> Decimal:
        """What the next entry may spend: settled cash plus the bridge's share of
        proceeds still settling. Can never exceed settled + unsettled."""
        return self.settled + min(self.bridge, self.unsettled)

    # computed_field, not a plain property: it is part of `engine cash --json`.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def next_settle(self) -> tuple[date, Decimal] | None:
        """The earliest proceeds still waiting, and how much of them there is."""
        return self.pending[0] if self.pending else None

    def spend(self, amount: Decimal) -> "CashLedger":
        """The ledger after a buy in this same pass. It comes out of settled cash,
        below zero when the bridge fronts it — unsettled proceeds themselves are
        exactly what a cash account may not spend, so they are left untouched."""
        return self.model_copy(update={"settled": self.settled - amount})

    def shortfall(self, symbol: str, cost: Decimal) -> str:
        """Why an entry costing `cost` was refused, naming when cash arrives.

        A refusal that does not say when it lifts reads as a broken engine; the
        settle date is what tells a reader this is the T+1 rule and it ends.
        """
        head = f"{symbol}: ${cost:,.2f} needed, ${self.settled:,.2f} settled"
        if self.bridge > 0:
            head += (
                f" + ${min(self.bridge, self.unsettled):,.2f} bridged"
                f" (${self.bridge:,.2f} bridge, ${self.bridged:,.2f} in use)"
            )
        nxt = self.next_settle
        if nxt is None:
            return f"{head} — nothing pending settlement (cash account)"
        when, amount = nxt
        return f"{head} — ${amount:,.2f} from earlier exits settles {when} (T+1, cash account)"


def ledger(
    capital: Decimal, flows: Iterable[Flow], today: date, bridge: Decimal | None = None
) -> CashLedger:
    """The ledger as of `today`, from a starting capital and every trade since.

    A buy costs `quantity x price + fees` on its trade date — the cash leaves at
    once. A sell brings in `quantity x price - fees`, settled once
    `settles_on(trade date) <= today` and pending until then. A partial exit is
    just a sell. Flows dated after `today` are not part of an as-of view and are
    ignored. `bridge` only changes what may be spent, never the arithmetic here.
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
        bridge=bridge or Decimal(0),
    )


# Sub-cent slack when asking whether a buy fit inside settled cash: fractional
# fills round, and a buy that overshoots settled cash by a fraction of a cent was
# not funded with anybody's unsettled proceeds.
FUNDING_TOLERANCE = Decimal("0.01")


def unsettled_funding(
    capital: Decimal,
    prior: Iterable[Flow],
    trade_date: date,
    cost: Decimal,
    bridge: Decimal | None = None,
) -> date | None:
    """When the unsettled cash a buy spent settles, or None if settled cash paid
    for all of it.

    Settled cash includes the bridge: at the broker it is the owner's own settled
    cash, so a buy it fronted was paid for in full and selling it the same day is
    no violation. Only the part beyond engine-settled cash and the bridge counts.

    `prior` is every flow before the buy, in order. The good-faith-violation test
    hangs off this: a cash account that sells a position before the cash which
    bought it has settled has sold something not yet paid for. The ledger refuses
    such buys in the first place (an entry needs settled cash), so for any book
    the engine built this returns None — it exists so the live exit path can say
    so loudly if that ever stops being true.
    """
    before = ledger(capital, prior, trade_date)
    short = cost - before.settled - min(bridge or Decimal(0), before.unsettled)
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
