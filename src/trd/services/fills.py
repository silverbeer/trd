"""Ingest a live engine's broker fills: intents resolved, txns written, positions
opened and closed at the price the broker actually gave.

A live scan writes order intents and fills nothing. This is the other half: a
snapshot of the brokerage's orders and trade approvals (written as-is by an agent
session — see docs/robinhood-mcp.md) is matched to those intents and applied. No
network, no provider, no credentials: the same split as `ReconcileService`, for
the same reason — the read is authenticated and interactive, the bookkeeping is
deterministic and has to be testable without a broker account.

Idempotent by construction. Every change is keyed on the intent's status and the
broker order id it records, so re-ingesting the same snapshot finds every intent
already where the snapshot puts it and writes nothing.
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

import duckdb

from trd.errors import TrdError
from trd.models import (
    QUANTITY_TOLERANCE,
    Account,
    AppliedIntent,
    ApprovalState,
    BookedPosition,
    BrokerApproval,
    BrokerOrder,
    BrokerOrderState,
    EngineConfig,
    FillConflict,
    FillIngestResult,
    FillSnapshot,
    Instrument,
    IntentStatus,
    OrderIntent,
    PositionStatus,
    Side,
    UnmatchedBrokerItem,
    intent_expiry_cutoff,
)
from trd.repos import AccountRepo, TransactionRepo
from trd.repos.engine import EngineConfigRepo, EnginePositionRepo, OrderIntentRepo
from trd.services.engine import entry_levels

# Order states the broker will not move again with nothing filled.
_DEAD_STATES = {BrokerOrderState.REJECTED, BrokerOrderState.FAILED}


class FillIngestService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.accounts = AccountRepo(conn)
        self.txns = TransactionRepo(conn)
        self.configs = EngineConfigRepo(conn)
        self.positions = EnginePositionRepo(conn)
        self.intents = OrderIntentRepo(conn)

    def _live(self) -> tuple[EngineConfig, Account]:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")
        if not config.is_live:
            raise TrdError(
                "This is a paper engine: it fills its own decisions and has no broker "
                "orders to ingest. 'trd engine fills' is for an engine created with --live."
            )
        account = next((a for a in self.accounts.list_all() if a.id == config.account_id), None)
        if account is None:
            raise TrdError("Engine account is missing. Run 'trd engine init' again.")
        return config, account

    def expire_stale(self, now: datetime) -> list[AppliedIntent]:
        """Expire open intents nothing at the broker took up before they lapsed.

        PENDING and AWAITING_APPROVAL only, created before this session, or today
        once it is 16:00 ET — when a trade approval lapses (measured: an approval
        raised in the morning carried expires_at 20:00Z). SUBMITTED is left alone:
        the broker has an order, and only its answer resolves it. Expiring is what
        releases the slot, the entry and the cash a stale buy was holding.
        """
        _, account = self._live()
        return self._expire(account, now)

    def _expire(self, account: Account, now: datetime) -> list[AppliedIntent]:
        lapsed = self.intents.expire_before(account.id, intent_expiry_cutoff(now), now)
        return [
            _applied(intent, instrument, IntentStatus.EXPIRED, note="lapsed unplaced/unapproved")
            for intent, instrument in lapsed
        ]

    def ingest(self, snapshot: FillSnapshot, now: datetime) -> FillIngestResult:
        """Apply a broker snapshot to the live engine's intents.

        Approvals first, then orders, so an approval that was declined and an
        order that was then cancelled agree on one outcome instead of the second
        overwriting the first. The expiry sweep runs last: an intent the snapshot
        shows the broker took up must not be expired for looking unplaced.
        """
        config, account = self._live()
        result = FillIngestResult(
            account=account.name,
            at=now,
            taken_at=snapshot.taken_at,
            orders_read=len(snapshot.orders),
            approvals_read=len(snapshot.approvals),
        )
        rows = self.intents.list_for(account.id)
        instruments = {intent.id: instrument for intent, instrument in rows}

        def index(key: str) -> dict[str, OrderIntent]:
            return {
                value: intent for intent, _ in rows if (value := getattr(intent, key)) is not None
            }

        by_ref = index("ref_id")
        by_approval = index("approval_id")
        by_order = index("broker_order_id")

        for approval in snapshot.approvals:
            intent = by_approval.get(approval.approval_id) or by_ref.get(approval.approval_id)
            if intent is None:
                result.unmatched.append(_unmatched_approval(approval))
                continue
            current = self._current(intent.id)
            self._apply_approval(current, instruments[intent.id], approval, now, result)

        for order in snapshot.orders:
            # With trade approvals on, the order's ref_id is the approval's id,
            # not the one trd minted (measured 2026-10-21) — so both are tried.
            intent = None
            if order.ref_id is not None:
                intent = by_ref.get(order.ref_id) or by_approval.get(order.ref_id)
            intent = intent or by_order.get(order.id)
            if intent is None:
                result.unmatched.append(_unmatched_order(order))
                continue
            current = self._current(intent.id)
            self._apply_order(config, account, current, instruments[intent.id], order, now, result)

        result.expired = self._expire(account, now)
        return result

    def _current(self, intent_id: int) -> OrderIntent:
        """The intent as stored now: an approval earlier in this same pass may
        have moved it."""
        found = self.intents.get(intent_id)
        assert found is not None
        return found

    # ------------------------------------------------------------- approvals

    def _apply_approval(
        self,
        intent: OrderIntent,
        instrument: Instrument,
        approval: BrokerApproval,
        now: datetime,
        result: FillIngestResult,
    ) -> None:
        proposed = approval.proposed_order
        if proposed is not None and (
            (proposed.symbol and proposed.symbol != instrument.symbol)
            or (proposed.side and proposed.side != intent.side)
        ):
            result.conflicts.append(
                FillConflict(
                    message=(
                        f"approval {approval.approval_id} proposes {proposed.side} "
                        f"{proposed.symbol}, but intent #{intent.id} is {intent.side.value} "
                        f"{instrument.symbol} — not applied"
                    ),
                    symbol=instrument.symbol,
                    side=intent.side,
                    intent_id=intent.id,
                    approval_id=approval.approval_id,
                )
            )
            return

        state = approval.state
        if state == ApprovalState.DECLINED:
            target, error = IntentStatus.CANCELLED, "approval declined"
        elif state == ApprovalState.EXPIRED:
            target, error = IntentStatus.EXPIRED, "approval expired"
        else:
            # PENDING, APPROVED, or a state this build does not know: still alive.
            # Only an intent nothing has answered yet moves; one with a broker
            # order already follows the order, not the approval.
            if intent.status != IntentStatus.PENDING:
                return
            updated = self.intents.update(
                intent.id,
                status=IntentStatus.AWAITING_APPROVAL,
                approval_id=approval.approval_id,
                submitted_at=intent.submitted_at or now,
            )
            result.applied.append(_applied(intent, instrument, updated.status))
            return

        if intent.status == target or not intent.is_open:
            return  # already resolved; a fill is never undone by an approval
        self.intents.update(
            intent.id,
            status=target,
            approval_id=approval.approval_id,
            error=error,
            resolved_at=now,
        )
        result.applied.append(_applied(intent, instrument, target, note=error))

    # ---------------------------------------------------------------- orders

    def _apply_order(
        self,
        config: EngineConfig,
        account: Account,
        intent: OrderIntent,
        instrument: Instrument,
        order: BrokerOrder,
        now: datetime,
        result: FillIngestResult,
    ) -> None:
        def conflict(message: str) -> None:
            result.conflicts.append(
                FillConflict(
                    message=message,
                    symbol=order.symbol,
                    side=order.side,
                    intent_id=intent.id,
                    order_id=order.id,
                )
            )

        if order.symbol != instrument.symbol or order.side != intent.side:
            conflict(
                f"order {order.id} is {order.side.value} {order.symbol}, but intent "
                f"#{intent.id} is {intent.side.value} {instrument.symbol} — not applied"
            )
            return
        if intent.broker_order_id is not None and intent.broker_order_id != order.id:
            conflict(
                f"intent #{intent.id} is already tied to broker order "
                f"{intent.broker_order_id}; order {order.id} claims it too — not applied"
            )
            return

        filled = order.cumulative_quantity
        if order.state == BrokerOrderState.FILLED or (
            order.state == BrokerOrderState.CANCELLED and filled > 0
        ):
            if intent.status == IntentStatus.FILLED:
                return  # applied on an earlier ingest
            self._fill(config, account, intent, instrument, order, now, result, conflict)
            return

        if not intent.is_open:
            # The broker is still working, or has given up on, an order trd already
            # resolved. Never re-open silently: someone should look.
            if order.state not in (*_DEAD_STATES, BrokerOrderState.CANCELLED):
                conflict(
                    f"broker order {order.id} is {order.state}, but intent #{intent.id} "
                    f"was already resolved as {intent.status.value}"
                )
            return

        if order.state in _DEAD_STATES:
            target = IntentStatus.REJECTED
            error = order.state + (f": {order.reject_reason}" if order.reject_reason else "")
        elif order.state == BrokerOrderState.CANCELLED:
            target, error = IntentStatus.CANCELLED, "cancelled"
        else:
            # queued / unconfirmed / confirmed / partially_filled, or a state this
            # build does not know: the broker holds a live order and nothing is
            # booked until it finishes.
            if intent.status == IntentStatus.SUBMITTED:
                return
            updated = self.intents.update(
                intent.id,
                status=IntentStatus.SUBMITTED,
                broker_order_id=order.id,
                submitted_at=intent.submitted_at or order.created_at or now,
            )
            result.applied.append(
                _applied(intent, instrument, updated.status, broker_order_id=order.id)
            )
            return

        self.intents.update(
            intent.id,
            status=target,
            broker_order_id=order.id,
            error=error,
            resolved_at=now,
        )
        result.applied.append(
            _applied(intent, instrument, target, broker_order_id=order.id, note=error)
        )

    def _fill(
        self,
        config: EngineConfig,
        account: Account,
        intent: OrderIntent,
        instrument: Instrument,
        order: BrokerOrder,
        now: datetime,
        result: FillIngestResult,
        conflict: Callable[[str], None],
    ) -> None:
        quantity = order.cumulative_quantity
        price = order.average_price
        if price is None and order.executions:
            notional = sum((e.price * e.quantity for e in order.executions), Decimal(0))
            price = notional / sum((e.quantity for e in order.executions), Decimal(0))
        if quantity <= 0 or price is None or price <= 0:
            conflict(f"order {order.id} is {order.state} with no usable quantity or price")
            return
        executed_at = order.executed_at or now
        fees = order.fees

        if intent.side == Side.BUY:
            if intent.atr_at_entry is None:
                conflict(f"buy intent #{intent.id} carries no entry plan (atr_at_entry)")
                return
            self.txns.insert(
                account_id=account.id,
                instrument_id=instrument.id,
                side=Side.BUY,
                quantity=quantity,
                price=price,
                fees=fees,
                executed_at=executed_at,
                note=f"engine {intent.strategy} entry (broker {order.id})",
            )
            # Re-derived from the price actually paid, the same way a paper fill
            # derives them from the bar it fills at — live R is measured from here.
            stop, target = entry_levels(price, intent.atr_at_entry, config.exit_params)
            position = self.positions.open(
                account_id=account.id,
                instrument_id=instrument.id,
                signal_id=intent.signal_id,
                strategy=intent.strategy,
                opened_at=executed_at,
                entry_price=price,
                quantity=quantity,
                stop_price=stop,
                target_price=target,
                atr_at_entry=intent.atr_at_entry,
                last_bar_date=executed_at.date(),
            )
            result.opened.append(
                BookedPosition(
                    position_id=position.id,
                    symbol=instrument.symbol,
                    strategy=intent.strategy,
                    quantity=quantity,
                    price=price,
                    stop_price=stop,
                    target_price=target,
                )
            )
        else:
            position = self.positions.get(intent.position_id) if intent.position_id else None
            if position is None or position.status == PositionStatus.CLOSED:
                conflict(
                    f"sell order {order.id} filled, but the position intent #{intent.id} "
                    f"sells is {'already closed' if position else 'unknown'} — no txn written"
                )
                return
            remaining = position.remaining_quantity
            if quantity > remaining + QUANTITY_TOLERANCE:
                conflict(
                    f"sell order {order.id} filled {quantity.normalize():f}, more than the "
                    f"{remaining.normalize():f} position #{position.id} still holds — "
                    "no txn written"
                )
                return
            self.txns.insert(
                account_id=account.id,
                instrument_id=instrument.id,
                side=Side.SELL,
                quantity=quantity,
                price=price,
                fees=fees,
                executed_at=executed_at,
                note=f"engine {intent.strategy} exit: {intent.rule} (broker {order.id})",
            )
            # The same two repo calls the paper exit path makes: all of what is
            # left closes the trade with the rule's reason; less than that is a
            # trim — the stop, target and slot stay where they are.
            booked = position.model_copy()
            if quantity >= remaining - QUANTITY_TOLERANCE:
                self.positions.close(position.id, executed_at, price, intent.reason, intent.rule)
                booked.book_exit(remaining, price)
                bucket = result.closed
            else:
                self.positions.trim(position.id, quantity, price)
                booked.book_exit(quantity, price)
                bucket = result.trimmed
            bucket.append(
                BookedPosition(
                    position_id=position.id,
                    symbol=instrument.symbol,
                    strategy=position.strategy,
                    quantity=quantity,
                    price=price,
                    stop_price=position.stop_price,
                    target_price=position.target_price,
                    rule=intent.rule,
                    pnl=(price - position.entry_price) * quantity,
                    r_multiple=position.r_multiple_at(price),
                )
            )

        self.intents.update(
            intent.id,
            status=IntentStatus.FILLED,
            broker_order_id=order.id,
            filled_quantity=quantity,
            fill_price=price,
            fees=fees,
            submitted_at=intent.submitted_at or order.created_at or executed_at,
            resolved_at=executed_at,
            # A partial fill then cancel is a fill of what executed; say so.
            error=None if order.state == BrokerOrderState.FILLED else order.state,
        )
        result.applied.append(
            _applied(
                intent,
                instrument,
                IntentStatus.FILLED,
                quantity=quantity,
                price=price,
                broker_order_id=order.id,
                note=None if order.state == BrokerOrderState.FILLED else "partial, then cancelled",
            )
        )


def _applied(
    intent: OrderIntent,
    instrument: Instrument,
    to_status: IntentStatus,
    quantity: Decimal | None = None,
    price: Decimal | None = None,
    broker_order_id: str | None = None,
    note: str | None = None,
) -> AppliedIntent:
    return AppliedIntent(
        intent_id=intent.id,
        symbol=instrument.symbol,
        side=intent.side,
        quantity=quantity if quantity is not None else intent.quantity,
        price=price,
        from_status=intent.status,
        to_status=to_status,
        broker_order_id=broker_order_id or intent.broker_order_id,
        note=note,
    )


def _unmatched_order(order: BrokerOrder) -> UnmatchedBrokerItem:
    return UnmatchedBrokerItem(
        kind="order",
        id=order.id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity if order.quantity is not None else order.cumulative_quantity,
        state=order.state,
    )


def _unmatched_approval(approval: BrokerApproval) -> UnmatchedBrokerItem:
    proposed = approval.proposed_order
    return UnmatchedBrokerItem(
        kind="approval",
        id=approval.approval_id,
        symbol=proposed.symbol if proposed else None,
        side=proposed.side if proposed else None,
        quantity=proposed.quantity if proposed else None,
        state=approval.state,
    )
