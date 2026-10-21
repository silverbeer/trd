"""Live engine mode, phase 1b (SB-1375): ingest broker fills into txns.

A live scan writes order intents; this is what turns the broker's answer into
the book. The snapshot is the brokerage's own order / trade-approval reads,
written as-is. These tests pin that a fill opens and closes positions at the
broker's price (with live R re-derived from it), that every terminal answer
releases what the intent was holding, that nothing unmatched or contradictory is
ever written, and that ingesting twice is ingesting once.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.config import get_settings
from trd.db.connection import connect
from trd.errors import EnginePositionConflictError, OpenIntentConflictError, TrdError
from trd.models import (
    FillSnapshot,
    IntentStatus,
    OrderIntent,
    PositionStatus,
    Side,
    intent_expiry_cutoff,
)
from trd.models.fills import exchange_local
from trd.repos import AccountRepo, InstrumentRepo, OrderIntentRepo, TransactionRepo
from trd.repos.engine import EnginePositionRepo
from trd.services import EngineService, FillIngestService, PortfolioService
from trd.services.backup import export_data, restore_data
from trd.services.engine import stop_distance

from .conftest import FakeProvider
from .test_engine_live import CAPITAL, MONDAY, _filled_position, _live, _names

runner = CliRunner()
D = Decimal

# 14:31Z on 2024-09-16 is 10:31 in New York (EDT, UTC-4).
FILLED_AT_UTC = "2024-09-16T14:31:00Z"
FILLED_AT = datetime(2024, 9, 16, 10, 31)
AFTER = datetime(2024, 9, 16, 11, 0)


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


@pytest.fixture
def fills(conn: duckdb.DuckDBPyConnection) -> FillIngestService:
    return FillIngestService(conn)


def _order(
    intent: OrderIntent,
    symbol: str,
    state: str = "filled",
    price: str = "101.25",
    quantity: Decimal | None = None,
    order_id: str = "ord-1",
    ref_id: str | None = None,
    side: str | None = None,
    fees: str = "0.000000",
) -> dict:
    """An order exactly as get_equity_orders returns it (strings, UTC, extras)."""
    qty = quantity if quantity is not None else intent.quantity
    filled = state == "filled" or (state == "cancelled" and quantity is not None)
    return {
        "id": order_id,
        "ref_id": ref_id if ref_id is not None else intent.ref_id,
        "symbol": symbol,
        "side": side or intent.side.value,
        "type": "market",
        "state": state,
        "quantity": str(intent.quantity),
        "cumulative_quantity": str(qty) if filled else "0.000000",
        "average_price": price if filled else None,
        "fees": fees,
        "dollar_based_amount": None,
        "created_at": "2024-09-16T14:00:00.341498Z",
        "last_transaction_at": FILLED_AT_UTC,
        "executions": (
            [
                {
                    "id": "ex-1",
                    "price": price,
                    "quantity": str(qty),
                    "timestamp": FILLED_AT_UTC,
                    "fees": fees,
                }
            ]
            if filled
            else []
        ),
        "extended_hours": False,  # unknown fields are ignored
    }


def _snap(orders: list[dict] | None = None, approvals: list[dict] | None = None) -> FillSnapshot:
    return FillSnapshot.model_validate(
        {"orders": orders or [], "approvals": approvals or [], "taken_at": FILLED_AT_UTC}
    )


def _buy_intent(engine: EngineService, conn, provider, *symbols: str, **live) -> OrderIntent:
    _names(conn, provider, *(symbols or ("AAA",)))
    _live(engine, list(symbols or ("AAA",)), **live)
    result = engine.scan(at=MONDAY)
    assert len(result.intents) == 1
    return result.intents[0].intent


def _intent(conn, intent_id: int) -> OrderIntent:
    found = OrderIntentRepo(conn).get(intent_id)
    assert found is not None
    return found


def _txns(conn, engine: EngineService):
    return TransactionRepo(conn).list_chronological(engine.account().id)


# ------------------------------------------------------------------- snapshot


def test_broker_timestamps_land_exchange_local():
    assert exchange_local(datetime.fromisoformat(FILLED_AT_UTC)) == FILLED_AT
    naive = datetime(2024, 9, 16, 10, 31)
    assert exchange_local(naive) == naive  # already the convention


def test_the_captured_broker_shape_parses_as_is():
    """The real 2026-10-21 get_equity_orders / get_trade_approvals rows."""
    snap = FillSnapshot.model_validate(
        {
            "orders": [
                {
                    "id": "6ad8b159-x",
                    "ref_id": "01a15e73-483c-7e08-8605-16c59ffc08fd",
                    "symbol": "ABBV",
                    "side": "sell",
                    "type": "market",
                    "state": "filled",
                    "quantity": "0.051266",
                    "cumulative_quantity": "0.051266",
                    "average_price": "287.630000",
                    "fees": "0.000000",
                    "dollar_based_amount": None,
                    "created_at": "2026-10-21T12:34:33.341498Z",
                    "last_transaction_at": "2026-10-21T13:30:01.493Z",
                    "executions": [
                        {
                            "id": "e",
                            "price": "287.630000",
                            "quantity": "0.051266",
                            "timestamp": "2026-10-21T13:30:01.493Z",
                            "fees": "0.000000",
                        }
                    ],
                }
            ],
            "approvals": [
                {
                    "approval_id": "01a15e73-483c-7e08-8605-16c59ffc08fd",
                    "state": "APPROVAL_STATE_PENDING",
                    "expires_at": "2026-10-21T20:00:00Z",
                    "proposed_order": {"symbol": "ABBV", "side": "sell", "quantity": "0.051266"},
                }
            ],
        }
    )
    order = snap.orders[0]
    assert order.side == Side.SELL and order.average_price == D("287.63")
    assert order.executed_at == datetime(2026, 10, 21, 9, 30, 1, 493000)
    approval = snap.approvals[0]
    assert approval.state == "PENDING"
    # Approvals lapse at the 16:00 close.
    assert approval.expires_at == datetime(2026, 10, 21, 16, 0)


# ------------------------------------------------------------------- buy fills


def test_a_buy_fill_opens_the_position_at_the_fill_price(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    config = engine.config()
    fill = D("101.25")
    assert fill != intent.ref_price

    result = fills.ingest(_snap([_order(intent, "AAA", fees="0.10")]), AFTER)
    assert result.ok and result.conflicts == []

    txns = _txns(conn, engine)
    assert len(txns) == 1
    assert txns[0].side == Side.BUY
    assert txns[0].quantity == intent.quantity
    assert txns[0].price == fill and txns[0].fees == D("0.10")
    assert txns[0].executed_at == FILLED_AT

    rows = engine.position_rows(open_only=True)
    assert len(rows) == 1
    position = rows[0].position
    assert intent.atr_at_entry is not None
    stop = fill - stop_distance(intent.atr_at_entry, config.exit_params)
    target = fill + (fill - stop) * D(str(config.exit_params["target_r"]))
    assert position.entry_price == fill
    assert position.quantity == intent.quantity
    assert abs(position.stop_price - stop) < D("0.000001")
    assert abs(position.target_price - target) < D("0.000001")
    # Live R is measured from what was paid, not from the scan's price.
    assert position.stop_price != intent.stop_price
    assert position.trail_high == fill
    assert position.opened_at == FILLED_AT
    assert position.signal_id == intent.signal_id
    assert position.strategy == intent.strategy
    assert position.atr_at_entry == intent.atr_at_entry

    resolved = _intent(conn, intent.id)
    assert resolved.status == IntentStatus.FILLED
    assert resolved.fill_price == fill and resolved.filled_quantity == intent.quantity
    assert resolved.fees == D("0.10")
    assert resolved.broker_order_id == "ord-1"
    assert resolved.resolved_at == FILLED_AT

    assert [o.symbol for o in result.opened] == ["AAA"]
    assert [(a.from_status, a.to_status) for a in result.applied] == [
        (IntentStatus.PENDING, IntentStatus.FILLED)
    ]
    # The cash now comes off the txn, not the reservation: spent once, not twice.
    cash = engine.cash()
    assert cash is not None
    assert cash.settled == CAPITAL - intent.quantity * fill - D("0.10")
    status = engine.status()
    assert status.open_intents == 0 and status.open_positions == 1


def test_an_order_carrying_the_approval_id_matches(engine, fills, conn, provider):
    """With trade approvals on, the broker sets the order's ref_id to the
    approval's id, not ours (measured 2026-10-21)."""
    intent = _buy_intent(engine, conn, provider)
    OrderIntentRepo(conn).update(intent.id, approval_id="appr-1")

    pending = {
        "approval_id": "appr-1",
        "state": "APPROVAL_STATE_PENDING",
        "expires_at": "2024-09-16T20:00:00Z",
        "proposed_order": {"symbol": "AAA", "side": "buy", "quantity": str(intent.quantity)},
    }
    first = fills.ingest(_snap(approvals=[pending]), AFTER)
    assert _intent(conn, intent.id).status == IntentStatus.AWAITING_APPROVAL
    assert [a.to_status for a in first.applied] == [IntentStatus.AWAITING_APPROVAL]

    approved = {**pending, "state": "APPROVAL_STATE_APPROVED"}
    order = _order(intent, "AAA", ref_id="appr-1")
    result = fills.ingest(_snap([order], [approved]), AFTER)
    assert result.ok
    assert _intent(conn, intent.id).status == IntentStatus.FILLED
    assert len(result.opened) == 1 and result.unmatched == []


def test_a_known_broker_order_id_matches(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    OrderIntentRepo(conn).update(intent.id, status=IntentStatus.SUBMITTED, broker_order_id="ord-9")
    order = _order(intent, "AAA", order_id="ord-9", ref_id="something-else")
    result = fills.ingest(_snap([order]), AFTER)
    assert result.ok and len(result.opened) == 1


def test_a_live_order_marks_the_intent_submitted_and_books_nothing(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    for state in ("queued", "confirmed", "partially_filled"):
        fills.ingest(_snap([_order(intent, "AAA", state=state)]), AFTER)
        stored = _intent(conn, intent.id)
        assert stored.status == IntentStatus.SUBMITTED
        assert stored.broker_order_id == "ord-1"
        assert stored.submitted_at == datetime(2024, 9, 16, 10, 0, 0, 341498)
    assert _txns(conn, engine) == []
    assert engine.position_rows(open_only=True) == []
    # Still holds its slot.
    assert engine.status().open_buy_intents == 1


def test_partial_fill_then_cancel_is_a_fill_of_what_executed(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    half = (intent.quantity / 2).quantize(D("0.000001"))
    result = fills.ingest(_snap([_order(intent, "AAA", state="cancelled", quantity=half)]), AFTER)
    assert result.ok
    stored = _intent(conn, intent.id)
    assert stored.status == IntentStatus.FILLED
    assert stored.filled_quantity == half
    assert engine.position_rows(open_only=True)[0].position.quantity == half


# ------------------------------------------------------------------ sell fills


def _sell_intent(engine: EngineService, conn, provider) -> tuple[OrderIntent, int]:
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"], max_positions=1)
    position = _filled_position(engine, conn)
    provider.add_symbol("AAA", price="185")  # through the 190 stop
    result = engine.scan(at=MONDAY)
    assert len(result.intents) == 1
    return result.intents[0].intent, position.id


def test_a_sell_fill_closes_the_position_with_the_intents_rule(engine, fills, conn, provider):
    intent, position_id = _sell_intent(engine, conn, provider)
    result = fills.ingest(_snap([_order(intent, "AAA", price="184.50")]), AFTER)
    assert result.ok

    position = EnginePositionRepo(conn).get(position_id)
    assert position is not None
    assert position.status == PositionStatus.CLOSED
    assert position.exit_rule == "stop"
    assert position.exit_reason == intent.reason
    assert position.exit_price == D("184.50")
    assert position.closed_at == FILLED_AT
    assert position.realized_pnl == (D("184.50") - D("200")) * D("10")

    txns = _txns(conn, engine)
    assert [t.side for t in txns] == [Side.BUY, Side.SELL]
    assert txns[-1].price == D("184.50") and txns[-1].quantity == D("10")
    assert [c.rule for c in result.closed] == ["stop"]
    assert result.trimmed == []
    assert engine.status().capacity == 1  # the slot is free once it actually sold


def test_a_partial_sell_fill_trims(engine, fills, conn, provider):
    intent, position_id = _sell_intent(engine, conn, provider)
    result = fills.ingest(_snap([_order(intent, "AAA", price="186", quantity=D("4"))]), AFTER)
    assert result.ok
    position = EnginePositionRepo(conn).get(position_id)
    assert position is not None
    assert position.status == PositionStatus.OPEN
    assert position.closed_quantity == D("4") and position.remaining_quantity == D("6")
    assert position.stop_price == D("190")  # the plan for the rest is untouched
    assert [t.quantity for t in result.trimmed] == [D("4")]
    assert result.closed == []
    assert _intent(conn, intent.id).filled_quantity == D("4")


def test_a_sell_for_a_closed_position_is_a_conflict_not_a_txn(engine, fills, conn, provider):
    intent, position_id = _sell_intent(engine, conn, provider)
    EnginePositionRepo(conn).close(position_id, MONDAY, D("190"), "elsewhere", "stop")
    before = len(_txns(conn, engine))
    result = fills.ingest(_snap([_order(intent, "AAA")]), AFTER)
    assert not result.ok
    assert "already closed" in result.conflicts[0].message
    assert len(_txns(conn, engine)) == before
    assert _intent(conn, intent.id).status == IntentStatus.PENDING


# ----------------------------------------------------------------- idempotency


def test_ingesting_the_same_snapshot_twice_changes_nothing(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    snap = _snap([_order(intent, "AAA")])
    fills.ingest(snap, AFTER)
    txns, positions = len(_txns(conn, engine)), len(engine.position_rows())
    stored = _intent(conn, intent.id)

    again = fills.ingest(snap, AFTER)
    assert again.ok
    assert again.applied == [] and again.opened == [] and again.expired == []
    assert len(_txns(conn, engine)) == txns == 1
    assert len(engine.position_rows()) == positions == 1
    assert _intent(conn, intent.id) == stored


# --------------------------------------------------------- terminal, no fill


@pytest.mark.parametrize(
    ("order_state", "approval_state", "expected"),
    [
        ("rejected", None, IntentStatus.REJECTED),
        ("failed", None, IntentStatus.REJECTED),
        ("cancelled", None, IntentStatus.CANCELLED),
        (None, "APPROVAL_STATE_DECLINED", IntentStatus.CANCELLED),
        (None, "APPROVAL_STATE_EXPIRED", IntentStatus.EXPIRED),
    ],
)
def test_a_terminal_answer_releases_the_slot_and_the_cash(
    engine, fills, conn, provider, order_state, approval_state, expected
):
    intent = _buy_intent(engine, conn, provider, max_positions=1)
    assert engine.status().capacity == 0
    cash = engine.cash()
    assert cash is not None and cash.settled == CAPITAL - intent.notional

    orders, approvals = [], []
    if order_state is not None:
        order = _order(intent, "AAA", state=order_state)
        order["reject_reason"] = "insufficient buying power" if order_state == "rejected" else None
        orders.append(order)
    else:
        OrderIntentRepo(conn).update(intent.id, approval_id="appr-1")
        approvals.append({"approval_id": "appr-1", "state": approval_state})
    result = fills.ingest(_snap(orders, approvals), AFTER)
    assert result.ok

    stored = _intent(conn, intent.id)
    assert stored.status == expected
    assert stored.resolved_at == AFTER
    if order_state == "rejected":
        assert stored.error == "rejected: insufficient buying power"
    assert _txns(conn, engine) == []
    status = engine.status()
    assert status.open_intents == 0 and status.capacity == 1
    released = engine.cash()
    assert released is not None and released.settled == CAPITAL


# -------------------------------------------------------- unmatched, conflicts


def test_an_unmatched_order_is_reported_and_never_written(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    stranger = _order(intent, "ZZZ", order_id="manual-1", ref_id="not-ours")
    approval = {"approval_id": "nobody", "state": "APPROVAL_STATE_PENDING"}
    result = fills.ingest(_snap([stranger], [approval]), AFTER)
    assert result.ok
    assert [(u.kind, u.id) for u in result.unmatched] == [
        ("approval", "nobody"),
        ("order", "manual-1"),
    ]
    assert _txns(conn, engine) == []
    assert _intent(conn, intent.id).status == IntentStatus.PENDING


@pytest.mark.parametrize(("symbol", "side"), [("BBB", "buy"), ("AAA", "sell")])
def test_a_symbol_or_side_mismatch_is_a_conflict(engine, fills, conn, provider, symbol, side):
    intent = _buy_intent(engine, conn, provider)
    result = fills.ingest(_snap([_order(intent, symbol, side=side)]), AFTER)
    assert not result.ok
    assert result.conflicts[0].intent_id == intent.id
    assert "not applied" in result.conflicts[0].message
    assert _txns(conn, engine) == []
    assert _intent(conn, intent.id).status == IntentStatus.PENDING


def test_a_paper_engine_refuses_fill_ingest(engine, fills, conn, provider):
    _names(conn, provider, "AAA")
    engine.init(symbols=["AAA"], strategies=["momentum"])
    with pytest.raises(TrdError, match="paper engine"):
        fills.ingest(_snap(), AFTER)
    with pytest.raises(TrdError, match="paper engine"):
        fills.expire_stale(AFTER)


# ---------------------------------------------------------------------- expiry


def test_expiry_cutoff_is_midnight_before_the_close_and_tomorrow_after():
    assert intent_expiry_cutoff(datetime(2024, 9, 16, 15, 59)) == datetime(2024, 9, 16)
    assert intent_expiry_cutoff(datetime(2024, 9, 16, 16, 0)) == datetime(2024, 9, 17)


def test_expire_stale_at_the_close(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    assert fills.expire_stale(datetime(2024, 9, 16, 15, 59)) == []
    assert _intent(conn, intent.id).status == IntentStatus.PENDING

    close = datetime(2024, 9, 16, 16, 0)
    expired = fills.expire_stale(close)
    assert [(e.intent_id, e.to_status) for e in expired] == [(intent.id, IntentStatus.EXPIRED)]
    stored = _intent(conn, intent.id)
    assert stored.status == IntentStatus.EXPIRED and stored.resolved_at == close
    assert fills.expire_stale(close) == []  # once


def test_expire_stale_on_a_later_session_including_awaiting_approval(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    OrderIntentRepo(conn).update(
        intent.id, status=IntentStatus.AWAITING_APPROVAL, approval_id="appr-1"
    )
    expired = fills.expire_stale(datetime(2024, 9, 17, 9, 0))
    assert [e.from_status for e in expired] == [IntentStatus.AWAITING_APPROVAL]


def test_a_submitted_intent_never_expires(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    OrderIntentRepo(conn).update(intent.id, status=IntentStatus.SUBMITTED, broker_order_id="o")
    assert fills.expire_stale(datetime(2024, 9, 20, 16, 30)) == []
    assert _intent(conn, intent.id).status == IntentStatus.SUBMITTED


def test_a_fill_in_the_snapshot_beats_the_sweep(engine, fills, conn, provider):
    """Ingest applies the snapshot before it sweeps: an intent the broker filled
    must never be expired for looking unplaced."""
    intent = _buy_intent(engine, conn, provider)
    result = fills.ingest(_snap([_order(intent, "AAA")]), datetime(2024, 9, 17, 9, 0))
    assert result.expired == []
    assert _intent(conn, intent.id).status == IntentStatus.FILLED


def test_a_live_scan_sweeps_stale_intents_before_it_trades(engine, conn, provider):
    intent = _buy_intent(engine, conn, provider, max_positions=1)
    # A new session: Monday's unplaced buy lapsed, so its slot and name are free.
    provider.add_symbol("AAA", price=str(float(intent.ref_price) * 1.002))
    tuesday = engine.scan(at=datetime(2024, 9, 17, 10, 0))
    assert _intent(conn, intent.id).status == IntentStatus.EXPIRED
    assert any("expired 1 stale intent" in s for s in tuesday.skipped), tuesday.skipped
    assert len(tuesday.intents) == 1
    assert tuesday.intents[0].intent.id != intent.id


# ------------------------------------------------------------ manual trades


def test_a_manual_trade_with_an_open_intent_is_refused(engine, conn, provider):
    _buy_intent(engine, conn, provider)
    portfolio = PortfolioService(conn, provider)
    with pytest.raises(OpenIntentConflictError, match="open order intent"):
        portfolio.record_trade("brokerage", "AAA", Side.BUY, D("1"), price=D("100"))
    # Caught by anything that already refuses an engine-held name.
    with pytest.raises(EnginePositionConflictError):
        portfolio.record_trade("brokerage", "AAA", Side.BUY, D("1"), price=D("100"))


def test_a_manual_trade_after_the_intent_resolves_is_allowed(engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    fills.ingest(_snap([_order(intent, "AAA", state="rejected")]), AFTER)
    portfolio = PortfolioService(conn, provider)
    txn = portfolio.record_trade("brokerage", "AAA", Side.BUY, D("1"), price=D("100"))
    assert txn.quantity == D("1")


# ---------------------------------------------------------------------- backup


def test_backup_round_trips_order_intents(tmp_path: Path, engine, fills, conn, provider):
    intent = _buy_intent(engine, conn, provider)
    fills.ingest(_snap([_order(intent, "AAA")]), AFTER)
    position = engine.position_rows(open_only=True)[0].position
    sell = OrderIntentRepo(conn).insert(
        account_id=engine.account().id,
        instrument_id=position.instrument_id,
        side=Side.SELL,
        quantity=position.quantity,
        ref_price=D("99"),
        reason="test exit",
        strategy=position.strategy,
        created_at=AFTER,
        rule="stop",
        position_id=position.id,
    )

    data = export_data(conn)
    assert data["version"] == 6
    assert len(data["engine"]["intents"]) == 2

    fresh = connect(tmp_path / "restored.duckdb")
    stats = restore_data(fresh, data)
    assert stats.order_intents == 2
    restored_engine = EngineService(fresh, provider)
    got = {i.ref_id: i for i, _ in OrderIntentRepo(fresh).list_for(restored_engine.account().id)}
    assert set(got) == {intent.ref_id, sell.ref_id}

    buy = got[intent.ref_id]
    original = _intent(conn, intent.id)
    assert buy.status == IntentStatus.FILLED
    assert buy.fill_price == original.fill_price and buy.broker_order_id == "ord-1"
    assert buy.atr_at_entry == original.atr_at_entry
    assert buy.signal_id is not None  # re-linked to the restored signal
    restored_position = restored_engine.position_rows(open_only=True)[0].position
    assert got[sell.ref_id].position_id == restored_position.id
    assert got[sell.ref_id].is_open
    assert restored_engine.status().open_intents == 1


def test_a_v5_backup_restores_with_no_intents(tmp_path: Path, engine, conn, provider):
    _buy_intent(engine, conn, provider)
    data = export_data(conn)
    data["version"] = 5
    del data["engine"]["intents"]
    fresh = connect(tmp_path / "restored.duckdb")
    assert restore_data(fresh, data).order_intents == 0
    assert fresh.execute("SELECT count(*) FROM order_intent").fetchone() == (0,)


# ------------------------------------------------------------------------- CLI


def _cli_live(cli_env: FakeProvider) -> OrderIntent:
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    created = runner.invoke(
        app,
        [
            "engine",
            "init",
            "--live",
            "--account",
            "brokerage",
            "--capital",
            "5000",
            "--symbols",
            "AAA",
        ],
    )
    assert created.exit_code == 0, created.output
    conn = connect(get_settings().db_path)
    account = AccountRepo(conn).get_by_name("brokerage")
    instrument = InstrumentRepo(conn).get_by_symbol("AAA")
    assert account is not None and instrument is not None
    intent = OrderIntentRepo(conn).insert(
        account_id=account.id,
        instrument_id=instrument.id,
        side=Side.BUY,
        quantity=D("1.5"),
        ref_price=D("100"),
        reason="test",
        strategy="momentum",
        created_at=datetime.now(),
        stop_price=D("90"),
        target_price=D("120"),
        atr_at_entry=D("5"),
    )
    conn.close()
    return intent


def test_engine_fills_json_shape(cli_env: FakeProvider, tmp_path: Path):
    intent = _cli_live(cli_env)
    snapshot = tmp_path / "fills.json"
    snapshot.write_text(json.dumps({"orders": [_order(intent, "AAA", price="100.40")]}))

    out = runner.invoke(app, ["engine", "fills", str(snapshot), "--json"])
    assert out.exit_code == 0, out.output
    doc = json.loads(out.output)
    assert {
        "account",
        "at",
        "taken_at",
        "orders_read",
        "approvals_read",
        "applied",
        "opened",
        "closed",
        "trimmed",
        "expired",
        "conflicts",
        "unmatched",
        "ok",
    } <= set(doc)
    assert doc["ok"] is True and doc["account"] == "brokerage"
    assert doc["applied"][0]["to_status"] == "filled"
    assert D(doc["opened"][0]["price"]) == D("100.40")
    assert D(doc["opened"][0]["stop_price"]) == D("90.40")  # 100.40 - 2 x 5

    again = runner.invoke(app, ["engine", "fills", str(snapshot), "--json"])
    assert again.exit_code == 0
    assert json.loads(again.output)["applied"] == []

    human = runner.invoke(app, ["engine", "fills", str(snapshot)])
    assert human.exit_code == 0 and "brokerage" in human.output


def test_engine_fills_exits_non_zero_on_a_conflict(cli_env: FakeProvider, tmp_path: Path):
    intent = _cli_live(cli_env)
    snapshot = tmp_path / "fills.json"
    snapshot.write_text(json.dumps({"orders": [_order(intent, "AAA", side="sell")]}))

    out = runner.invoke(app, ["engine", "fills", str(snapshot), "--json"])
    assert out.exit_code == 1
    doc = json.loads(out.output)
    assert doc["ok"] is False and len(doc["conflicts"]) == 1

    human = runner.invoke(app, ["engine", "fills", str(snapshot)])
    assert human.exit_code == 1 and "Conflicts" in human.output


def test_engine_fills_refuses_a_paper_engine_and_a_bad_file(cli_env: FakeProvider, tmp_path):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert runner.invoke(app, ["engine", "init", "--symbols", "AAA"]).exit_code == 0
    snapshot = tmp_path / "fills.json"
    snapshot.write_text(json.dumps({"orders": []}))

    out = runner.invoke(app, ["engine", "fills", str(snapshot), "--json"])
    assert out.exit_code == 1
    assert "paper engine" in json.loads(out.output)["message"]

    snapshot.write_text(json.dumps({"orders": [{"id": "x"}]}))
    bad = runner.invoke(app, ["engine", "fills", str(snapshot), "--json"])
    assert bad.exit_code == 1
    assert set(json.loads(bad.output)) == {"error", "message"}
