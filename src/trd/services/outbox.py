"""Drain a live engine's outbox: pending intents out as order specs, the broker's
answer back in.

A live scan writes order intents and places nothing. An agent session reads the
outbox, places each order through the broker's own tools, and acks what came back
— an approval id, a broker order id, or the error the place call returned. trd
never reaches the broker (docs/robinhood-mcp.md): the spec it emits and the id it
records are the whole of its side.

The ack is the agent's word for what happened at the moment of placing; the fill
snapshot (`FillIngestService`) is the broker's word for what happened after. Both
move an intent through the same two helpers, so either path leaves the row in the
same shape, and the snapshot can always finish what an ack started.
"""

from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

import duckdb

from trd.errors import IntentAckError, TrdError
from trd.models import (
    EXPIRABLE_INTENT_STATUSES,
    Account,
    EngineConfig,
    Instrument,
    IntentStatus,
    OrderIntent,
    Outbox,
    OutboxAck,
    OutboxOrder,
    Side,
    WithheldOrder,
    intent_expires_at,
    intent_expiry_cutoff,
    intent_is_late,
)
from trd.models.engine import STALE_INTENT_MINUTES
from trd.repos import AccountRepo
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo, OrderIntentRepo
from trd.services.fills import live_engine, mark_awaiting_approval, mark_submitted
from trd.services.guard import CapTally, GuardService

# The broker takes fractional shares to six places. Rounded down, so a spec never
# asks for more than the engine sized — on a sell, more than the position holds.
SHARE_QUANTUM = Decimal("0.000001")


class OutboxService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.accounts = AccountRepo(conn)
        self.configs = EngineConfigRepo(conn)
        self.intents = OrderIntentRepo(conn)
        self.positions = EnginePositionRepo(conn)
        self.guards = GuardService(conn)

    def _live(self) -> tuple[EngineConfig, Account]:
        return live_engine(
            self.configs,
            self.accounts,
            "This is a paper engine: it fills its own decisions and has no outbox. "
            "'trd engine outbox' is for an engine created with --live.",
        )

    def pending(self, now: datetime, include_stale: bool = False) -> Outbox:
        """Every PENDING intent still actionable at `now`, oldest first, as the
        order the broker should be asked for. Reads only — an intent that has
        lapsed is left out, not expired: the next scan or fill ingest sweeps it.

        The guardrails are applied here a second time, against the caps as they
        stand NOW (a cap lowered after an intent was written still binds it):

        - halted: nothing at all, sells included. `orders` is empty and `halted`
          says why — an agent that reads only `orders` places nothing.
        - a buy that breaks a hard cap is `withheld`, with the cap it breaks.
        - a buy older than STALE_INTENT_MINUTES is `withheld` as stale, unless
          `include_stale` — then it is listed, flagged in its note, and still
          checked against the caps.
        """
        config, account = self._live()
        guard = self.guards.live_guard(config)
        if guard.halted:
            return Outbox(account=account.name, at=now, halted=True, halt_reason=guard.halt_reason)
        cutoff = intent_expiry_cutoff(now)
        actionable = [
            (intent, instrument)
            for intent, instrument in reversed(self.intents.list_for(account.id))
            if intent.is_open
        ]
        pending = [
            (intent, instrument)
            for intent, instrument in actionable
            if intent.status == IntentStatus.PENDING and intent.created_at >= cutoff
        ]
        # What is already committed without this outbox: today's buying minus the
        # pending buys about to be counted one by one, and the slots held by
        # positions and by buys the broker already has.
        pending_buys = sum(
            (
                i.notional
                for i, _ in pending
                if i.side == Side.BUY and i.created_at.date() == now.date()
            ),
            Decimal(0),
        )
        placed_buys = sum(
            1 for i, _ in actionable if i.side == Side.BUY and i.status != IntentStatus.PENDING
        )
        tally = CapTally(
            guard,
            self.intents.buy_notional_on(account.id, now.date()) - pending_buys,
            len(self.positions.list_open(account.id)) + placed_buys,
        )

        orders: list[OutboxOrder] = []
        withheld: list[WithheldOrder] = []
        stale_before = now - timedelta(minutes=STALE_INTENT_MINUTES)
        for intent, instrument in pending:
            spec = _spec(intent, instrument, now)
            if intent.side != Side.BUY:
                orders.append(spec)
                continue
            if intent.created_at < stale_before:
                stale = f"stale — sized off a quote from {intent.created_at:%H:%M}"
                if not include_stale:
                    withheld.append(WithheldOrder(order=spec, reason=stale, stale=True))
                    continue
                spec.note = f"{stale}; {spec.note}" if spec.note else stale
            refused = tally.refuse(instrument.symbol, intent.notional)
            if refused is not None:
                withheld.append(WithheldOrder(order=spec, reason=refused))
                continue
            tally.take(intent.notional)
            orders.append(spec)
        return Outbox(account=account.name, at=now, orders=orders, withheld=withheld)

    def ack(
        self,
        key: str,
        now: datetime,
        *,
        approval_id: str | None = None,
        broker_order_id: str | None = None,
        error: str | None = None,
    ) -> OutboxAck:
        """Record what the broker answered when the agent placed an intent.

        Exactly one answer per call. Repeating an answer already recorded changes
        nothing; an answer that contradicts the record — a different approval or
        order id, or any answer to an intent already resolved — is refused with an
        IntentAckError, never written over.
        """
        answers = [v for v in (approval_id, broker_order_id, error) if v]
        if len(answers) != 1:
            raise TrdError("Give exactly one of --approval-id, --broker-order-id or --error.")
        _, account = self._live()
        found = self.intents.find(account.id, key)
        if found is None:
            raise TrdError(
                f"No order intent {key!r} on '{account.name}'. "
                "'trd engine outbox' lists the pending ones by id and ref_id."
            )
        intent, instrument = found
        if approval_id:
            updated = self._approval(intent, approval_id, now)
        elif broker_order_id:
            updated = self._order(intent, broker_order_id, now)
        else:
            assert error is not None
            updated = self._error(intent, error, now)
        changed = updated is not None
        after = updated or intent
        return OutboxAck(
            symbol=instrument.symbol,
            from_status=intent.status,
            to_status=after.status,
            changed=changed,
            intent=after,
        )

    def _approval(self, intent: OrderIntent, approval_id: str, now: datetime) -> OrderIntent | None:
        if intent.approval_id == approval_id:
            return None  # recorded already — by an earlier ack or by a fill snapshot
        if intent.approval_id is not None:
            raise IntentAckError(
                f"Intent #{intent.id} already waits on approval {intent.approval_id}; "
                f"refusing {approval_id}. One intent is one order: a retry reuses the "
                "intent's ref_id and never places a second one."
            )
        if intent.status != IntentStatus.PENDING:
            raise _refused(intent, "an approval")
        return mark_awaiting_approval(self.intents, intent, approval_id, now)

    def _order(
        self, intent: OrderIntent, broker_order_id: str, now: datetime
    ) -> OrderIntent | None:
        if intent.broker_order_id == broker_order_id:
            return None
        if intent.broker_order_id is not None:
            raise IntentAckError(
                f"Intent #{intent.id} is already tied to broker order "
                f"{intent.broker_order_id}; refusing {broker_order_id}."
            )
        if intent.status not in EXPIRABLE_INTENT_STATUSES:
            raise _refused(intent, "a broker order")
        return mark_submitted(self.intents, intent, broker_order_id, now)

    def _error(self, intent: OrderIntent, error: str, now: datetime) -> OrderIntent | None:
        if intent.status == IntentStatus.REJECTED and intent.error == error:
            return None
        if intent.status != IntentStatus.PENDING:
            raise _refused(intent, "a place error")
        return self.intents.update(
            intent.id, status=IntentStatus.REJECTED, error=error, resolved_at=now
        )


def _refused(intent: OrderIntent, answer: str) -> IntentAckError:
    if intent.is_open:
        why = (
            "it has already been placed, so only the broker's answer resolves it now "
            "('trd engine fills')"
        )
    else:
        why = "it is resolved, and a resolved intent is never reopened"
    return IntentAckError(
        f"Intent #{intent.id} is {intent.status.value}; refusing {answer}: {why}."
    )


def _spec(intent: OrderIntent, instrument: Instrument, now: datetime) -> OutboxOrder:
    expires_at = intent_expires_at(intent.created_at)
    late = intent_is_late(intent.created_at)
    note = None
    if late:
        left = max(0, int((expires_at - now).total_seconds() // 60))
        note = (
            f"written {intent.created_at:%H:%M} ET: an approval placed now has {left} "
            f"minute(s) before it expires at {expires_at:%H:%M} ET"
        )
    return OutboxOrder(
        intent_id=intent.id,
        ref_id=intent.ref_id,
        symbol=instrument.symbol,
        side=intent.side,
        quantity=f"{intent.quantity.quantize(SHARE_QUANTUM, rounding=ROUND_DOWN):f}",
        ref_price=intent.ref_price,
        notional=intent.notional,
        strategy=intent.strategy,
        rule=intent.rule,
        reason=intent.reason,
        created_at=intent.created_at,
        expires_at=expires_at,
        late=late,
        note=note,
    )
