"""A live engine's guardrails: the kill switch and the hard caps.

The strategy config says what the engine WANTS to trade. These say what trd will
let it trade, whatever the config says — the limits that have to exist before
trade approvals are switched off and nobody reads an order before it fills
(SB-1377).

Enforced twice, on purpose. The live scan will not write an intent that breaks a
cap, and the outbox will not hand over one that breaks a cap NOW — caps can be
lowered after an intent was written, and the outbox is the last thing trd says
before an agent places an order. Both sides count through `CapTally`, so the two
checks cannot disagree about what "today's buying" is.

Every cap is on BUYS. A sell is never withheld by a cap: an exit that cannot be
placed leaves a position unmanaged, which is the very risk the caps bound. The
kill switch is the exception, and it is total — halted, the outbox emits nothing
at all, because "stop" has to mean stop.

Pure bookkeeping over the database: no provider, no network. The loss stop is
marked with the quotes a scan already holds, or the last stored close when it
has none (fill ingest), the same marks `engine status` uses.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb

from trd.errors import TrdError
from trd.models import (
    Account,
    EngineConfig,
    EngineGuard,
    HaltCause,
    IntentStatus,
    OrderIntent,
    Quote,
    intent_expiry_cutoff,
)
from trd.repos import PriceRepo
from trd.repos.engine import EngineGuardRepo, EnginePositionRepo

# Defaults `init --live` writes, as multiples of the config they guard.
# The most one buy may cost: a quarter above the position size. Exposure sizing
# never asks for more than the size itself, so only an order sized off a
# different config — or a bug — reaches this.
GUARD_ORDER_MULTIPLE = Decimal("1.25")
# The loss at which the engine halts itself, as a fraction of its capital.
GUARD_MAX_LOSS_FRACTION = Decimal("0.20")

# Sessions an intent may sit SUBMITTED at the broker before it counts as stuck.
# Robinhood's market orders are good-for-day: one the broker still holds after the
# session it was placed in means the snapshot that would resolve it never came.
STUCK_SUBMITTED_SESSIONS = 1

PAPER_REFUSAL = (
    "This is a paper engine: it fills its own decisions in a simulation account and "
    "places nothing at a broker, so there is nothing to halt or cap. The kill switch "
    "and hard caps are for an engine created with --live."
)


def default_guard(config: EngineConfig) -> EngineGuard:
    """The caps a live engine gets when nobody set any: what `init --live` writes,
    and what a live engine with no stored row (one created before migration 028)
    is held to."""
    capital = config.capital
    return EngineGuard(
        account_id=config.account_id,
        max_order=config.position_size * GUARD_ORDER_MULTIPLE,
        max_daily=capital,
        max_positions=config.max_positions,
        max_loss=capital * GUARD_MAX_LOSS_FRACTION if capital is not None else None,
    )


class CapTally:
    """One pass's running count against the caps: today's buying and the slots
    in use. Built once, consulted per order, advanced per order taken — so a pass
    that writes three buys counts the first two against the third."""

    def __init__(self, guard: EngineGuard, spent_today: Decimal, slots_in_use: int) -> None:
        self.guard = guard
        self.spent_today = spent_today
        self.slots_in_use = slots_in_use

    def refuse(self, symbol: str, notional: Decimal) -> str | None:
        """Why a buy of `notional` breaks a cap, or None when it fits."""
        guard = self.guard
        if guard.max_order is not None and notional > guard.max_order:
            return (
                f"{symbol}: hard cap — a ${notional:,.2f} order is over max-order "
                f"${guard.max_order:,.2f}"
            )
        if guard.max_daily is not None and self.spent_today + notional > guard.max_daily:
            return (
                f"{symbol}: hard cap — ${notional:,.2f} on top of ${self.spent_today:,.2f} "
                f"bought today is over max-daily ${guard.max_daily:,.2f}"
            )
        if guard.max_positions is not None and self.slots_in_use + 1 > guard.max_positions:
            return (
                f"{symbol}: hard cap — {self.slots_in_use} position(s) and open buys "
                f"already, max-positions is {guard.max_positions}"
            )
        return None

    def take(self, notional: Decimal) -> None:
        self.spent_today += notional
        self.slots_in_use += 1


def stuck_intents(intents: list[OrderIntent], now: datetime) -> int:
    """Open intents nothing has moved when something should have.

    SUBMITTED from an earlier session than the last STUCK_SUBMITTED_SESSIONS: a
    good-for-day order the broker should have filled or cancelled by its close.
    AWAITING_APPROVAL past its 16:00 expiry: the approval has lapsed at the broker,
    and the intent still holds its slot and cash until a scan or ingest sweeps it.
    Both mean the loop between trd and the broker has stopped turning.
    """
    submitted_before = _sessions_back(now.date(), STUCK_SUBMITTED_SESSIONS - 1)
    lapsed_before = intent_expiry_cutoff(now)
    count = 0
    for intent in intents:
        if intent.status == IntentStatus.SUBMITTED:
            placed = intent.submitted_at or intent.created_at
            if placed.date() < submitted_before:
                count += 1
        elif intent.status == IntentStatus.AWAITING_APPROVAL and intent.created_at < lapsed_before:
            count += 1
    return count


def _sessions_back(day: date, sessions: int) -> date:
    """The weekday `sessions` weekdays before `day` (0 is `day` itself)."""
    while sessions > 0:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            sessions -= 1
    return day


class GuardService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.guards = EngineGuardRepo(conn)
        self.positions = EnginePositionRepo(conn)
        self.prices = PriceRepo(conn)

    def guard(self, config: EngineConfig) -> EngineGuard | None:
        """The guard in force: the stored row, else the defaults. None on paper,
        which has nothing at a broker to guard."""
        if not config.is_live:
            return None
        return self.guards.get(config.account_id) or default_guard(config)

    def live_guard(self, config: EngineConfig) -> EngineGuard:
        guard = self.guard(config)
        if guard is None:
            raise TrdError(PAPER_REFUSAL)
        return guard

    def seed(self, config: EngineConfig, at: datetime) -> EngineGuard | None:
        """Write the defaults for a live engine that has no row yet. A re-init
        leaves an existing row alone: its caps were chosen, and a kill switch that
        `init` released would be no kill switch."""
        if not config.is_live or self.guards.get(config.account_id) is not None:
            return self.guard(config)
        return self.guards.save(default_guard(config), at)

    def halt(
        self,
        config: EngineConfig,
        reason: str | None,
        at: datetime,
        cause: HaltCause = HaltCause.MANUAL,
    ) -> EngineGuard:
        guard = self.live_guard(config)
        text = (reason or "").strip() or "halted by hand"
        return self.guards.save(
            guard.model_copy(
                update={"halted": True, "halt_reason": text, "halted_at": at, "halted_by": cause}
            ),
            at,
        )

    def resume(self, config: EngineConfig, at: datetime) -> EngineGuard:
        guard = self.live_guard(config)
        return self.guards.save(
            guard.model_copy(
                update={"halted": False, "halt_reason": None, "halted_at": None, "halted_by": None}
            ),
            at,
        )

    def set_caps(
        self,
        config: EngineConfig,
        at: datetime,
        max_order: Decimal | None = None,
        max_daily: Decimal | None = None,
        max_positions: int | None = None,
        max_loss: Decimal | None = None,
    ) -> EngineGuard:
        """Change the named caps and leave the rest. A cap cannot be switched off:
        each one is the last line between a bug and a real account."""
        guard = self.live_guard(config)
        update: dict[str, object] = {}
        for name, value in (
            ("max_order", max_order),
            ("max_daily", max_daily),
            ("max_positions", max_positions),
            ("max_loss", max_loss),
        ):
            if value is None:
                continue
            if value <= 0:
                raise TrdError(
                    f"--{name.replace('_', '-')} must be positive. A hard cap cannot be "
                    "switched off; set it as high as you mean it."
                )
            update[name] = value
        if not update:
            return guard
        return self.guards.save(guard.model_copy(update=update), at)

    # ----------------------------------------------------------- the loss stop

    def book_pnl(
        self, account: Account, quotes: dict[str, Quote] | None = None
    ) -> tuple[Decimal, Decimal]:
        """(realized, unrealized) for the engine account — the same two halves
        `engine status` reports. Open positions are marked at the quote when the
        caller has one, else at the last stored close."""
        quotes = quotes or {}
        realized = Decimal(0)
        unrealized = Decimal(0)
        for position, _ in self.positions.list_closed(account.id):
            realized += position.booked_pnl
        for position, instrument in self.positions.list_open(account.id):
            realized += position.booked_pnl
            quote = quotes.get(instrument.symbol)
            mark = quote.price if quote is not None else self.prices.latest_close(instrument.id)
            unrealized += position.pnl_at(mark) or Decimal(0)
        return realized, unrealized

    def check_loss(
        self,
        config: EngineConfig,
        account: Account,
        at: datetime,
        quotes: dict[str, Quote] | None = None,
    ) -> str | None:
        """Halt the engine when realized + unrealized has reached -max_loss.

        Returns the halt reason when THIS call tripped it, so the caller alerts
        exactly once; None when the engine is within its limit or already halted.
        """
        guard = self.guard(config)
        if guard is None or guard.halted or guard.max_loss is None:
            return None
        realized, unrealized = self.book_pnl(account, quotes)
        net = realized + unrealized
        if net > -guard.max_loss:
            return None
        reason = (
            f"loss stop: realized {realized:+,.2f} + unrealized {unrealized:+,.2f} = "
            f"{net:+,.2f}, at or past max-loss -${guard.max_loss:,.2f}. The engine halted "
            f"itself at {at:%Y-%m-%d %H:%M}; 'trd engine resume' restarts it, and the next "
            "scan halts it again unless max-loss is raised"
        )
        self.halt(config, reason, at, cause=HaltCause.MAX_LOSS)
        return reason
