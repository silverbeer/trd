"""Human slots on a live engine (SB-1742): the owner picks the entry, the engine
manages the exit.

A human slot is an ordinary `engine_position` with strategy `human`. The owner
asks for a buy (`trd engine human buy SYM`); trd sizes it to one human slot,
plans the stop and target from ATR exactly as a scan's entry would, and writes a
buy intent the live runner places like any other — the phone approval is still
the gate. From the fill on, the position is the engine's to exit: stop, trail,
target, indicator and time, the same rules as every engine position. "Let it
run" (`trd engine human run SYM`) skips the target and the time exit, so a winner
leaves on the trail.

One capital, two sleeves. The human sleeve's capital is part of the engine
config's total; the engine sleeve is the rest, so adding human slots raises the
total and leaves the engine's buying power untouched. Each sleeve has its own
T+1 ledger and bridge (`EngineService.human_cash`), its own loss stop, and its
own slot count. The engine's guard is the engine's: its halt and caps never
withhold a human order, and a human loss never halts the engine.

Live only. Every live fill is the fill of an intent, which is what lets the
human sleeve's cash be replayed exactly from its intents without marking txns.
"""

from datetime import datetime
from decimal import Decimal

import duckdb
from pydantic import BaseModel

from trd.engine.bars import DAILY
from trd.engine.settlement import CashLedger, ledger
from trd.errors import NotTradableError, TrdError
from trd.models import (
    HUMAN_MAX_LOSS_FRACTION,
    HUMAN_STRATEGY,
    EngineHuman,
    OrderIntentRow,
    PositionRow,
    Side,
    SizingMode,
    intent_expires_at,
    is_human,
)
from trd.providers.base import MarketDataProvider
from trd.services.engine import EngineService, plan_entry
from trd.services.sync import SyncService
from trd.timeframes import SESSION_OPEN_MINUTE

# Daily bars a human buy needs before it can size a stop: ATR(14) wants 15.
MIN_BARS = 15
# What a name with too little history gets pulled, in years — the same depth
# 'trd engine add' pulls, so the exit rules have their lookbacks.
BACKFILL_YEARS = 2

LIVE_ONLY = (
    "Human slots are for the live engine: every live fill is a broker fill on an order "
    "intent, which is how the human sleeve's cash is kept apart from the engine's. A "
    "paper engine has none."
)


class HumanView(BaseModel):
    """`trd engine human` — the sleeve, its cash and its positions."""

    account: str
    sleeve: EngineHuman | None = None
    capital_total: Decimal | None = None
    engine_capital: Decimal | None = None
    cash: CashLedger | None = None
    used: int = 0
    open_buys: int = 0
    positions: list[PositionRow] = []
    let_run: list[str] = []
    realized: Decimal = Decimal(0)
    unrealized: Decimal = Decimal(0)


class HumanBuy(BaseModel):
    """What one `trd engine human buy` wrote: a buy intent the runner places."""

    symbol: str
    intent: OrderIntentRow
    size: Decimal
    buying_power: Decimal
    backfilled: int = 0


class HumanService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.engine = EngineService(conn, provider)

    # ----------------------------------------------------------------- reads

    def view(self) -> HumanView:
        config = self.engine.config()
        account = self.engine.account()
        sleeve = self.engine.human_sleeve(config, account)
        if not config.is_live:
            raise TrdError(LIVE_ONLY)
        rows = [r for r in self.engine.position_rows() if is_human(r.position.strategy)]
        let_run = self.engine.let_run.ids()
        realized, unrealized = self.engine.guards.book_pnl(account, human=True)
        return HumanView(
            account=account.name,
            sleeve=sleeve,
            capital_total=config.capital,
            engine_capital=(
                config.capital - sleeve.capital
                if config.capital is not None and sleeve is not None
                else config.capital
            ),
            cash=self.engine.human_cash(),
            used=len(rows),
            open_buys=self._open_buys(account.id),
            positions=rows,
            let_run=[r.instrument.symbol for r in rows if r.position.id in let_run],
            realized=realized,
            unrealized=unrealized,
        )

    # ---------------------------------------------------------------- writes

    def set(
        self,
        at: datetime,
        slots: int | None = None,
        capital: Decimal | None = None,
        bridge: Decimal | None = None,
        no_bridge: bool = False,
        max_loss: Decimal | None = None,
    ) -> HumanView:
        """Create or change the human sleeve. Only what is passed changes.

        A new human capital raises the TOTAL capital by the difference, so the
        engine sleeve (total - human) keeps exactly what it had.
        """
        config = self.engine.config()
        account = self.engine.account()
        if not config.is_live:
            raise TrdError(LIVE_ONLY)
        if config.capital is None:
            raise TrdError("The live engine has no capital. Set one: 'trd engine cash --set'.")
        current = self.engine.humans.get(account.id)
        if current is None and (slots is None or capital is None):
            raise TrdError(
                "First time: give both --slots and --capital, e.g. "
                "'trd engine human set --slots 2 --capital C'."
            )
        if slots is not None and slots < 1:
            raise TrdError("--slots must be at least 1.")
        if capital is not None and capital <= 0:
            raise TrdError("--capital must be positive.")
        if bridge is not None and bridge < 0:
            raise TrdError("--bridge cannot be negative. Use --no-bridge to switch it off.")
        if max_loss is not None and max_loss <= 0:
            raise TrdError("--max-loss must be positive — set it as high as you mean it.")

        old_capital = current.capital if current is not None else Decimal(0)
        new_capital = capital if capital is not None else old_capital
        new_slots = slots if slots is not None else (current.slots if current else 0)
        in_use = self._in_use(account.id)
        if new_slots < in_use:
            raise TrdError(
                f"{in_use} human slot(s) are in use (positions and open buys); --slots "
                f"cannot go below that. Close one first ('trd engine close SYM')."
            )
        new_bridge = (
            None
            if no_bridge
            else (
                bridge if bridge is not None else (current.bridge if current is not None else None)
            )
        )
        default_for = lambda c: (c * HUMAN_MAX_LOSS_FRACTION).quantize(Decimal("0.01"))  # noqa: E731
        if max_loss is not None:
            new_max_loss = max_loss
        elif (
            current is not None
            and current.max_loss is not None
            and (capital is None or current.max_loss != default_for(old_capital))
        ):
            # A loss stop someone set by hand stays; only the default follows the capital.
            new_max_loss = current.max_loss
        else:
            new_max_loss = default_for(new_capital)

        if current is not None and new_capital < old_capital:
            # The sleeve's ledger replays its fills from its capital; below what it
            # has already spent it would open negative and refuse every buy.
            account_flows = self.engine._human_flows(account)
            history = ledger(Decimal(0), account_flows, at.date())
            spent = -(history.settled + history.unsettled)
            if new_capital < spent:
                raise TrdError(
                    f"The human sleeve has {spent:,.2f} tied up (net of sales); --capital "
                    f"{new_capital:,.2f} is below that. Close a position first."
                )

        if new_capital != old_capital:
            # Validated by set_capital against everything the account has spent.
            self.engine.set_capital(config.capital - old_capital + new_capital)
        self.engine.humans.save(
            EngineHuman(
                account_id=account.id,
                slots=new_slots,
                capital=new_capital,
                bridge=new_bridge,
                max_loss=new_max_loss,
                halted=current.halted if current is not None else False,
                halt_reason=current.halt_reason if current is not None else None,
                halted_at=current.halted_at if current is not None else None,
            ),
            at,
        )
        return self.view()

    def buy(self, symbol: str, at: datetime, note: str | None = None) -> HumanBuy:
        """Ask for a human buy: a buy intent sized to one human slot, with the
        engine's own entry plan. Nothing is bought until the broker fills it.

        Every refusal comes before any write. Refused outside the session: an
        intent sits PENDING until the runner's next pass, and one written before
        the open would be stale (sized off yesterday's close) by the time it ran.
        """
        config = self.engine.config()
        account = self.engine.account()
        if not config.is_live:
            raise TrdError(LIVE_ONLY)
        sleeve = self.engine.humans.get(account.id)
        if sleeve is None:
            raise TrdError(
                "No human slots on this engine. Create them: "
                "'trd engine human set --slots 2 --capital C'."
            )
        if sleeve.halted:
            raise TrdError(
                f"The human sleeve is halted: {sleeve.halt_reason} "
                "'trd engine human resume' lifts it."
            )
        minute = at.hour * 60 + at.minute
        if at.weekday() > 4 or minute < SESSION_OPEN_MINUTE or at >= intent_expires_at(at):
            raise TrdError(
                "Human buys are taken during the session only (09:30-16:00 ET, weekdays): "
                "an order written outside it would be sized off a stale price and lapse "
                "before the runner could place it."
            )
        tripped = self._check_loss(sleeve, at)
        if tripped is not None:
            raise TrdError(tripped)

        wanted = symbol.upper()
        instrument = self.engine.instruments.get_by_symbol(wanted)
        if instrument is None:
            instrument = self.engine.instruments.insert(self.provider.get_info(wanted))
        if not instrument.tradable:
            raise NotTradableError(instrument.symbol)
        for position, held in self.engine.positions.list_open(account.id):
            if held.id == instrument.id:
                owner = "a human slot" if is_human(position.strategy) else "the engine"
                raise TrdError(
                    f"{instrument.symbol} is already held by {owner}. A name has one owner "
                    "at a time — FIFO on one account would mix their lots."
                )
        if self.engine.intents.has_open(account.id, instrument.id):
            raise TrdError(
                f"An order for {instrument.symbol} is already in flight "
                "('trd engine intents'). Wait for it to resolve."
            )
        in_use = self._in_use(account.id)
        if in_use >= sleeve.slots:
            raise TrdError(
                f"All {sleeve.slots} human slot(s) are in use. Close one "
                "('trd engine close SYM') or add a slot ('trd engine human set --slots N')."
            )

        backfilled = 0
        source = self.engine.bars(config)
        stored = source.stored(instrument.id)
        if len(stored) < MIN_BARS:
            if config.timeframe != DAILY:
                raise TrdError(f"{instrument.symbol}: not enough bars to size a stop.")
            backfilled = SyncService(self.conn, self.provider).backfill_symbol(
                instrument.symbol, years=BACKFILL_YEARS
            )
            stored = source.stored(instrument.id)
        quote = self.engine._quotes([instrument.symbol]).get(instrument.symbol)
        if quote is None:
            raise TrdError(f"No quote for {instrument.symbol} — try again in a moment.")
        bars = source.with_live_bar(stored, quote, at)
        plan, skip = plan_entry(bars, sleeve.size, config.exit_params, SizingMode.EXPOSURE)
        if plan is None:
            raise TrdError(f"{instrument.symbol}: {skip or 'cannot be sized'}.")
        price = bars[-1].close
        cost = plan.quantity * price
        cash = self.engine.human_cash(at.date())
        assert cash is not None
        if cost > cash.buying_power:
            raise TrdError(cash.shortfall(instrument.symbol, cost) + " (human sleeve)")

        reason = "human pick" + (f": {note}" if note else "")
        intent = self.engine.intents.insert(
            account_id=account.id,
            instrument_id=instrument.id,
            side=Side.BUY,
            quantity=plan.quantity,
            ref_price=price,
            reason=reason,
            strategy=HUMAN_STRATEGY,
            created_at=at,
            stop_price=plan.stop_price,
            target_price=plan.target_price,
            atr_at_entry=plan.atr,
        )
        return HumanBuy(
            symbol=instrument.symbol,
            intent=OrderIntentRow(intent=intent, instrument=instrument),
            size=sleeve.size,
            buying_power=cash.buying_power,
            backfilled=backfilled,
        )

    def let_run(self, symbol: str, at: datetime, on: bool = True) -> bool:
        """Flag (or unflag) a human position "let it run". True when it changed."""
        account = self.engine.account()
        wanted = symbol.upper()
        for position, instrument in self.engine.positions.list_open(account.id):
            if instrument.symbol.upper() != wanted:
                continue
            if not is_human(position.strategy):
                raise TrdError(
                    f"{instrument.symbol} is an engine position. Let-run is for human slots: "
                    "overriding a rule's exit would blur what the rule is measured on."
                )
            if on:
                return self.engine.let_run.set(position.id, at)
            return self.engine.let_run.clear(position.id)
        raise TrdError(
            f"No open human position in {wanted}. 'trd engine human' lists them; a buy "
            "still waiting for its fill can be flagged once it has filled."
        )

    def resume(self, at: datetime) -> EngineHuman:
        account = self.engine.account()
        sleeve = self.engine.humans.get(account.id)
        if sleeve is None:
            raise TrdError("No human slots on this engine.")
        return self.engine.humans.save(
            sleeve.model_copy(update={"halted": False, "halt_reason": None, "halted_at": None}),
            at,
        )

    # --------------------------------------------------------------- helpers

    def _open_buys(self, account_id: int) -> int:
        return sum(
            1
            for intent, _ in self.engine.intents.list_open(account_id)
            if intent.side == Side.BUY and is_human(intent.strategy)
        )

    def _in_use(self, account_id: int) -> int:
        """Human slots taken: open human positions plus human buys in flight."""
        held = sum(
            1 for p, _ in self.engine.positions.list_open(account_id) if is_human(p.strategy)
        )
        return held + self._open_buys(account_id)

    def _check_loss(self, sleeve: EngineHuman, at: datetime) -> str | None:
        """The human loss stop, marked at the live quote as the scan marks the
        engine's: the stored close is yesterday's, and a stop is about now."""
        account = self.engine.account()
        held = sorted(
            {
                i.symbol
                for p, i in self.engine.positions.list_open(account.id)
                if is_human(p.strategy)
            }
        )
        quotes = self.engine._quotes(held) if held else {}
        return self.engine.guards.check_human_loss(account, at, quotes)


__all__ = ["HumanBuy", "HumanService", "HumanView"]
