"""Live engine 1c (SB-1376): the agent-drained order outbox.

A live scan writes order intents; an agent session drains them. `outbox` hands
each pending intent over as the broker's order parameters, `outbox ack` records
what the place call returned, and the fill snapshot finishes the job. These tests
pin the spec (pending only, nothing expired, late flagged), every ack transition
and its refusals, the hand-off from an acked approval to a booked fill, the text
of each live Telegram message, and where each one is sent. Telegram is stubbed at
urlopen — nothing here reaches the network.
"""

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

import trd.cli.app as cli
from trd.cli.app import app
from trd.config import get_settings
from trd.db.connection import connect
from trd.errors import IntentAckError, TrdError
from trd.models import (
    AppliedIntent,
    BookedPosition,
    FillConflict,
    FillIngestResult,
    Instrument,
    InstrumentType,
    IntentStatus,
    OrderIntent,
    OrderIntentRow,
    OutboxAck,
    Side,
)
from trd.notify.messages import (
    approval_message,
    fill_alert_message,
    fill_messages,
    intent_message,
    scan_alert_messages,
    scan_messages,
)
from trd.notify.telegram import LIVE_LABEL, alert_notifier_from_env, label_from_env
from trd.repos import AccountRepo, InstrumentRepo, OrderIntentRepo
from trd.services import EngineService, FillIngestService, OutboxService
from trd.services.engine import ScanResult

from .conftest import FakeProvider
from .test_engine_fills import AFTER, _buy_intent, _order, _snap
from .test_engine_live import MONDAY, _live, _names

runner = CliRunner()
D = Decimal

SESSION = datetime(2026, 10, 21, 10, 0)
NOW = datetime(2026, 10, 21, 10, 5)
SIZED = D("0.0694219")
TELEGRAM_ENV = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TRD_LIVE_ALERT_CHAT_ID",
    "TRD_BOT_ALLOWED_USER_IDS",
    "TRD_ENGINE_LABEL",
)


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """No inherited Telegram config, and every send captured instead of posted."""
    for name in TELEGRAM_ENV:
        monkeypatch.delenv(name, raising=False)
    sent: list[dict] = []

    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        sent.append(json.loads(request.data))
        return _Response()

    monkeypatch.setattr("trd.notify.telegram.urllib.request.urlopen", fake_urlopen)
    return sent


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


@pytest.fixture
def outbox(conn: duckdb.DuckDBPyConnection) -> OutboxService:
    return OutboxService(conn)


def _write(
    conn,
    account_id: int,
    created_at: datetime,
    symbol: str = "AAA",
    side: Side = Side.BUY,
    quantity: Decimal = SIZED,
    status: IntentStatus | None = None,
) -> OrderIntent:
    instrument = InstrumentRepo(conn).get_by_symbol(symbol)
    assert instrument is not None
    repo = OrderIntentRepo(conn)
    intent = repo.insert(
        account_id=account_id,
        instrument_id=instrument.id,
        side=side,
        quantity=quantity,
        ref_price=D("288.10"),
        reason="close broke the 20-session high",
        strategy="momentum",
        created_at=created_at,
        rule="stop" if side == Side.SELL else None,
        stop_price=D("280") if side == Side.BUY else None,
        target_price=D("300") if side == Side.BUY else None,
        atr_at_entry=D("4") if side == Side.BUY else None,
    )
    if status is not None:
        intent = repo.update(intent.id, status=status)
    return intent


def _live_engine(engine: EngineService, conn, provider) -> int:
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"])
    return engine.account().id


def _stored(conn, intent_id: int) -> OrderIntent:
    found = OrderIntentRepo(conn).get(intent_id)
    assert found is not None
    return found


# --------------------------------------------------------------------- outbox


def test_outbox_lists_pending_intents_as_order_specs(engine, outbox, conn, provider):
    account_id = _live_engine(engine, conn, provider)
    first = _write(conn, account_id, SESSION)
    second = _write(conn, account_id, SESSION.replace(minute=2), symbol="BBB", side=Side.SELL)
    _write(conn, account_id, SESSION, status=IntentStatus.SUBMITTED)
    _write(conn, account_id, SESSION, status=IntentStatus.AWAITING_APPROVAL)
    _write(conn, account_id, SESSION, status=IntentStatus.FILLED)

    result = outbox.pending(NOW)
    assert result.account == "brokerage" and result.at == NOW
    assert [o.intent_id for o in result.orders] == [first.id, second.id]  # oldest first

    spec = result.orders[0]
    assert spec.ref_id == first.ref_id and spec.symbol == "AAA" and spec.side == Side.BUY
    # Six places, rounded down: never more than the engine sized.
    assert spec.quantity == "0.069421"
    assert (spec.type, spec.time_in_force, spec.market_hours) == (
        "market",
        "gfd",
        "regular_hours",
    )
    assert spec.ref_price == D("288.10") and spec.notional == first.notional
    assert spec.expires_at == datetime(2026, 10, 21, 16, 0)
    assert spec.late is False and spec.note is None
    assert result.orders[1].rule == "stop"


def test_outbox_leaves_out_expired_intents_and_writes_nothing(engine, outbox, conn, provider):
    account_id = _live_engine(engine, conn, provider)
    stale = _write(conn, account_id, datetime(2026, 10, 20, 15, 0))  # an earlier session
    today = _write(conn, account_id, SESSION)

    assert [o.intent_id for o in outbox.pending(NOW).orders] == [today.id]
    # After the close, today's lapses too.
    assert outbox.pending(datetime(2026, 10, 21, 16, 1)).orders == []
    # Read-only: the sweep belongs to the scan and the fill ingest.
    assert _stored(conn, stale.id).status == IntentStatus.PENDING


def test_outbox_flags_a_late_intent_and_keeps_it(engine, outbox, conn, provider):
    account_id = _live_engine(engine, conn, provider)
    _write(conn, account_id, datetime(2026, 10, 21, 15, 44))
    late = _write(conn, account_id, datetime(2026, 10, 21, 15, 45))

    orders = outbox.pending(datetime(2026, 10, 21, 15, 52)).orders
    assert [o.late for o in orders] == [False, True]
    assert orders[1].intent_id == late.id
    assert orders[1].note is not None and "8 minute(s)" in orders[1].note
    assert "16:00" in orders[1].note


def test_a_paper_engine_has_no_outbox(engine, outbox, conn, provider):
    _names(conn, provider, "AAA")
    engine.init(symbols=["AAA"], strategies=["momentum"])
    with pytest.raises(TrdError, match="paper engine"):
        outbox.pending(NOW)
    with pytest.raises(TrdError, match="paper engine"):
        outbox.ack("1", NOW, approval_id="x")


# ------------------------------------------------------------------------ ack


def test_ack_approval_moves_pending_to_awaiting_approval(engine, outbox, conn, provider):
    intent = _write(conn, _live_engine(engine, conn, provider), SESSION)

    ack = outbox.ack(str(intent.id), NOW, approval_id="appr-1")
    assert ack.changed and ack.symbol == "AAA"
    assert (ack.from_status, ack.to_status) == (
        IntentStatus.PENDING,
        IntentStatus.AWAITING_APPROVAL,
    )
    stored = _stored(conn, intent.id)
    assert stored.approval_id == "appr-1" and stored.submitted_at == NOW
    assert stored.ref_id == intent.ref_id  # never re-minted

    again = outbox.ack(intent.ref_id, NOW, approval_id="appr-1")  # by ref_id, repeated
    assert not again.changed and again.to_status == IntentStatus.AWAITING_APPROVAL

    with pytest.raises(IntentAckError, match="appr-1"):
        outbox.ack(str(intent.id), NOW, approval_id="appr-2")
    assert _stored(conn, intent.id).approval_id == "appr-1"


@pytest.mark.parametrize("approved_first", [False, True])
def test_ack_broker_order_moves_to_submitted(engine, outbox, conn, provider, approved_first):
    intent = _write(conn, _live_engine(engine, conn, provider), SESSION)
    if approved_first:
        outbox.ack(str(intent.id), NOW, approval_id="appr-1")

    ack = outbox.ack(str(intent.id), NOW, broker_order_id="ord-1")
    assert ack.changed and ack.to_status == IntentStatus.SUBMITTED
    stored = _stored(conn, intent.id)
    assert stored.broker_order_id == "ord-1" and stored.submitted_at == NOW
    if approved_first:
        assert stored.approval_id == "appr-1"

    assert not outbox.ack(str(intent.id), NOW, broker_order_id="ord-1").changed
    with pytest.raises(IntentAckError, match="ord-1"):
        outbox.ack(str(intent.id), NOW, broker_order_id="ord-2")


def test_ack_error_rejects_a_pending_intent(engine, outbox, conn, provider):
    intent = _write(conn, _live_engine(engine, conn, provider), SESSION)

    ack = outbox.ack(str(intent.id), NOW, error="insufficient buying power")
    assert ack.changed and ack.to_status == IntentStatus.REJECTED
    stored = _stored(conn, intent.id)
    assert stored.error == "insufficient buying power" and stored.resolved_at == NOW
    # Releases the slot like any terminal answer.
    assert engine.status().open_intents == 0

    assert not outbox.ack(str(intent.id), NOW, error="insufficient buying power").changed
    with pytest.raises(IntentAckError, match="rejected"):
        outbox.ack(str(intent.id), NOW, error="something else")


def test_ack_error_refuses_an_intent_already_placed(engine, outbox, conn, provider):
    intent = _write(conn, _live_engine(engine, conn, provider), SESSION)
    outbox.ack(str(intent.id), NOW, approval_id="appr-1")
    with pytest.raises(IntentAckError, match="broker's answer"):
        outbox.ack(str(intent.id), NOW, error="late failure")
    assert _stored(conn, intent.id).status == IntentStatus.AWAITING_APPROVAL


@pytest.mark.parametrize(
    "status", [IntentStatus.FILLED, IntentStatus.EXPIRED, IntentStatus.CANCELLED]
)
def test_ack_refuses_a_terminal_intent(engine, outbox, conn, provider, status):
    intent = _write(conn, _live_engine(engine, conn, provider), SESSION, status=status)
    for answer in ({"approval_id": "a"}, {"broker_order_id": "o"}, {"error": "e"}):
        with pytest.raises(IntentAckError, match="never reopened"):
            outbox.ack(str(intent.id), NOW, **answer)
    assert _stored(conn, intent.id).status == status


def test_ack_needs_one_answer_and_a_known_intent(engine, outbox, conn, provider):
    intent = _write(conn, _live_engine(engine, conn, provider), SESSION)
    with pytest.raises(TrdError, match="exactly one"):
        outbox.ack(str(intent.id), NOW)
    with pytest.raises(TrdError, match="exactly one"):
        outbox.ack(str(intent.id), NOW, approval_id="a", broker_order_id="o")
    with pytest.raises(TrdError, match="No order intent"):
        outbox.ack("999", NOW, approval_id="a")


def test_an_acked_approval_resolves_from_the_fill_snapshot(engine, outbox, conn, provider):
    """End to end: the agent acks the approval, the person approves it, and the
    order that comes back carries the approval's id as its ref_id."""
    intent = _buy_intent(engine, conn, provider)
    assert [o.intent_id for o in outbox.pending(MONDAY).orders] == [intent.id]

    outbox.ack(intent.ref_id, MONDAY, approval_id="appr-1")
    assert outbox.pending(MONDAY).orders == []  # placed: no longer in the outbox

    approved = {
        "approval_id": "appr-1",
        "state": "APPROVAL_STATE_APPROVED",
        "proposed_order": {"symbol": "AAA", "side": "buy", "quantity": str(intent.quantity)},
    }
    order = _order(intent, "AAA", ref_id="appr-1", price="101.25")
    result = FillIngestService(conn).ingest(_snap([order], [approved]), AFTER)
    assert result.ok and result.unmatched == []
    assert _stored(conn, intent.id).status == IntentStatus.FILLED
    assert len(result.opened) == 1

    # The fill pushes against the price the scan saw.
    (message,) = fill_messages(result, LIVE_LABEL)
    slip = (D("101.25") - intent.ref_price) / intent.ref_price * 100
    assert message.startswith(f"[💵 live] filled BUY AAA {intent.quantity.normalize():f} @ 101.25")
    assert f"scan saw {float(intent.ref_price):,.2f}, {float(slip):+.2f}%" in message

    # A late ack after the fill is the same answer, already recorded.
    assert not outbox.ack(intent.ref_id, AFTER, approval_id="appr-1").changed


# ------------------------------------------------------------------- messages


def _row(side: Side = Side.BUY, quantity: str = "0.069") -> OrderIntentRow:
    intent = OrderIntent(
        id=7,
        account_id=1,
        instrument_id=1,
        side=side,
        quantity=D(quantity),
        ref_price=D("288.10"),
        notional=D("20.00"),
        reason="close broke the 20-session high" if side == Side.BUY else "close 279 < stop 280",
        rule="stop" if side == Side.SELL else None,
        strategy="momentum",
        ref_id="ref-7",
        created_at=SESSION,
    )
    return OrderIntentRow(
        intent=intent, instrument=Instrument(id=1, symbol="XYZ", type=InstrumentType.STOCK)
    )


def test_intent_message_for_a_live_buy():
    text = intent_message(_row(), LIVE_LABEL)
    assert text.splitlines()[0] == (
        "💵 LIVE: BUY XYZ 0.069 sh (~$20.00 @ 288.10) — Momentum. Waiting for the outbox; "
        "approve in Robinhood when it arrives, expires 16:00"
    )
    assert text.splitlines()[1] == "close broke the 20-session high"


def test_intent_message_for_a_live_sell_names_the_exit_rule():
    head = intent_message(_row(Side.SELL), LIVE_LABEL).splitlines()[0]
    assert head.startswith("💵 LIVE: SELL XYZ 0.069 sh (~$20.00 @ 288.10) — Stop Loss exit.")


def test_a_configured_label_is_kept_on_live_messages():
    assert intent_message(_row(), "live").startswith("[live] 💵 LIVE: BUY XYZ")


def test_intent_message_never_rounds_the_size():
    assert "0.069421 sh" in intent_message(_row(quantity="0.069421"), LIVE_LABEL)


def test_approval_message():
    row = _row(Side.SELL)
    ack = OutboxAck(
        symbol="XYZ",
        from_status=IntentStatus.PENDING,
        to_status=IntentStatus.AWAITING_APPROVAL,
        changed=True,
        intent=row.intent,
    )
    lines = approval_message(ack, LIVE_LABEL).splitlines()
    assert lines[0] == (
        "💵 LIVE: approve SELL XYZ 0.069 sh (~$20.00 @ 288.10) in the Robinhood app "
        "— expires 16:00 ET"
    )
    assert lines[1] == "Stop Loss exit: close 279 < stop 280"


def _fill(side: Side, price: str, status=IntentStatus.FILLED, note=None) -> AppliedIntent:
    return AppliedIntent(
        intent_id=7,
        symbol="XYZ",
        side=side,
        quantity=D("0.069"),
        price=D(price) if status == IntentStatus.FILLED else None,
        from_status=IntentStatus.SUBMITTED,
        to_status=status,
        note=note,
        ref_price=D("288.10"),
        position_id=3,
    )


def test_fill_message_for_a_buy():
    result = FillIngestResult(account="b", at=NOW, applied=[_fill(Side.BUY, "288.40")])
    assert fill_messages(result, LIVE_LABEL) == [
        "[💵 live] filled BUY XYZ 0.069 @ 288.40 (scan saw 288.10, +0.10%)"
    ]


def test_fill_message_for_a_sell_carries_realized_pnl_and_r():
    closed = BookedPosition(
        position_id=3,
        symbol="XYZ",
        strategy="momentum",
        quantity=D("0.069"),
        price=D("287.00"),
        pnl=D("-1.23"),
        r_multiple=D("-0.45"),
    )
    result = FillIngestResult(
        account="b", at=NOW, applied=[_fill(Side.SELL, "287.00")], closed=[closed]
    )
    (text,) = fill_messages(result, LIVE_LABEL)
    assert text.splitlines() == [
        "[💵 live] filled SELL XYZ 0.069 @ 287.00 (scan saw 288.10, -0.38%)",
        "realized -1.23 (-0.45R)",
    ]


def test_fill_alert_names_rejected_expired_and_conflicts():
    result = FillIngestResult(
        account="b",
        at=NOW,
        applied=[
            _fill(Side.BUY, "0", IntentStatus.REJECTED, "rejected: insufficient buying power"),
            _fill(Side.BUY, "0", IntentStatus.CANCELLED, "approval declined"),
        ],
        expired=[_fill(Side.SELL, "0", IntentStatus.EXPIRED, "lapsed unplaced/unapproved")],
        conflicts=[FillConflict(message="order ord-9 is sell XYZ, but intent #7 is buy")],
    )
    text = fill_alert_message(result, LIVE_LABEL)
    assert text is not None
    lines = text.splitlines()
    assert lines[0] == "💵 LIVE: 3 order(s) not booked"
    assert "REJECTED BUY XYZ 0.069 — rejected: insufficient buying power" in lines
    assert "EXPIRED SELL XYZ 0.069 — lapsed unplaced/unapproved" in lines
    assert "CONFLICT — order ord-9 is sell XYZ, but intent #7 is buy" in lines
    assert not any("CANCELLED" in line for line in lines)

    clean = FillIngestResult(account="b", at=NOW, applied=[_fill(Side.BUY, "288.40")])
    assert fill_alert_message(clean, LIVE_LABEL) is None
    assert fill_messages(FillIngestResult(account="b", at=NOW, applied=result.applied)) == []


def test_scan_intents_go_to_the_alert_list_not_the_channel_list():
    result = ScanResult(run_id=1, at=NOW, paper=True, scanned=1, intents=[_row()])
    assert scan_messages(result, LIVE_LABEL) == []
    assert scan_alert_messages(result, LIVE_LABEL) == [intent_message(_row(), LIVE_LABEL)]


def test_a_scan_sends_its_intents_only_when_asked(monkeypatch: pytest.MonkeyPatch):
    """The runner's approval request is the one message per order; the scan's
    'waiting for the outbox' is opt-in, for an outbox drained by hand."""
    sent: list[tuple[list[str], bool]] = []
    monkeypatch.setattr(cli, "_send", lambda messages, alert=False: sent.append((messages, alert)))
    result = ScanResult(run_id=1, at=NOW, paper=True, scanned=1, intents=[_row()])

    monkeypatch.delenv("TRD_LIVE_INTENT_ALERTS", raising=False)
    cli._notify_scan(result, LIVE_LABEL)
    assert not any(alert for _, alert in sent)

    sent.clear()
    monkeypatch.setenv("TRD_LIVE_INTENT_ALERTS", "1")
    cli._notify_scan(result, LIVE_LABEL)
    assert ([intent_message(_row(), LIVE_LABEL)], True) in sent


# --------------------------------------------------------------- destinations


def test_live_label_unless_one_is_configured():
    assert label_from_env(None, env={}, live=True) == LIVE_LABEL
    assert label_from_env({"flat_at_minute": 1555.0}, env={}, live=True) == LIVE_LABEL
    assert label_from_env(None, env={"TRD_ENGINE_LABEL": "live"}, live=True) == "live"
    assert label_from_env(None, env={}) == "swing"  # paper unchanged


def test_alert_chat_fallback_order():
    warnings: list[str] = []
    base = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "-100"}

    def chat(env: dict[str, str]) -> str | None:
        # The channel fallback is opt-in since SB-1377 (a live call to action is
        # refused rather than posted publicly); this pins the order when it is on.
        notifier = alert_notifier_from_env(env, warn=warnings.append, channel_fallback=True)
        return notifier.chat_id if notifier else None

    allowed = {**base, "TRD_BOT_ALLOWED_USER_IDS": "111, 222"}
    assert chat({**allowed, "TRD_LIVE_ALERT_CHAT_ID": "555"}) == "555"
    assert chat(allowed) == "111"
    assert warnings == []
    assert chat(base) == "-100"
    assert len(warnings) == 1 and "TELEGRAM_CHAT_ID" in warnings[0]
    assert chat({"TELEGRAM_BOT_TOKEN": "tok"}) is None
    assert chat({"TRD_LIVE_ALERT_CHAT_ID": "555"}) is None  # no token, no notifier


def test_a_live_scan_sends_intents_to_the_alert_chat(monkeypatch, _no_telegram):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    monkeypatch.setenv("TRD_LIVE_INTENT_ALERTS", "1")
    result = ScanResult(run_id=1, at=NOW, paper=True, scanned=1, intents=[_row()])
    cli._notify_scan(result, LIVE_LABEL)
    assert [(m["chat_id"], m["text"].split(":")[0]) for m in _no_telegram] == [("555", "💵 LIVE")]


# ------------------------------------------------------------------------ CLI


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 10, 21, 10, 5)


def _cli_live(cli_env: FakeProvider, monkeypatch) -> OrderIntent:
    monkeypatch.setattr(cli, "datetime", _Frozen)
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
    assert account is not None
    intent = _write(conn, account.id, SESSION, quantity=D("1.5"))
    conn.close()
    return intent


def test_engine_outbox_json(cli_env: FakeProvider, monkeypatch):
    intent = _cli_live(cli_env, monkeypatch)
    out = runner.invoke(app, ["engine", "outbox", "--json"])
    assert out.exit_code == 0, out.output
    doc = json.loads(out.output)
    assert doc["account"] == "brokerage"
    (spec,) = doc["orders"]
    assert spec["intent_id"] == intent.id and spec["ref_id"] == intent.ref_id
    assert spec["quantity"] == "1.500000" and spec["side"] == "buy"
    assert spec["type"] == "market" and spec["time_in_force"] == "gfd"
    assert spec["market_hours"] == "regular_hours"
    assert spec["expires_at"] == "2026-10-21T16:00:00" and spec["late"] is False

    human = runner.invoke(app, ["engine", "outbox"])
    assert human.exit_code == 0 and "AAA" in human.output


def test_engine_outbox_ack_json_and_refusals(cli_env: FakeProvider, monkeypatch, _no_telegram):
    intent = _cli_live(cli_env, monkeypatch)
    ack = ["engine", "outbox", "ack", str(intent.id)]

    first = runner.invoke(app, [*ack, "--approval-id", "appr-1", "--json"])
    assert first.exit_code == 0, first.output
    doc = json.loads(first.output)
    assert doc["changed"] is True and doc["to_status"] == "awaiting_approval"
    assert doc["intent"]["approval_id"] == "appr-1"

    again = runner.invoke(app, [*ack, "--approval-id", "appr-1", "--json"])
    assert again.exit_code == 0 and json.loads(again.output)["changed"] is False

    clash = runner.invoke(app, [*ack, "--approval-id", "appr-2", "--json"])
    assert clash.exit_code == 1
    err = json.loads(clash.output)
    assert set(err) == {"error", "message"} and err["error"] == "IntentAckError"

    none = runner.invoke(app, [*ack, "--json"])
    assert none.exit_code == 1 and set(json.loads(none.output)) == {"error", "message"}

    human = runner.invoke(app, [*ack, "--broker-order-id", "ord-1"])
    assert human.exit_code == 0 and "awaiting_approval -> submitted" in human.output
    assert _no_telegram == []  # nothing asked to notify


def test_engine_outbox_ack_notify_asks_for_the_approval(
    cli_env: FakeProvider, monkeypatch, _no_telegram
):
    intent = _cli_live(cli_env, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TRD_BOT_ALLOWED_USER_IDS", "4242")
    args = ["engine", "outbox", "ack", intent.ref_id, "--approval-id", "appr-1", "--notify"]

    out = runner.invoke(app, args)
    assert out.exit_code == 0, out.output
    (message,) = _no_telegram
    assert message["chat_id"] == "4242"  # the private chat, not the channel
    assert message["text"].startswith("💵 LIVE: approve BUY AAA 1.5 sh")

    # A repeated ack changes nothing and asks nobody twice.
    assert runner.invoke(app, args).exit_code == 0
    assert len(_no_telegram) == 1


def test_engine_outbox_refuses_a_paper_engine(cli_env: FakeProvider):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert runner.invoke(app, ["engine", "init", "--symbols", "AAA"]).exit_code == 0
    for args in (["engine", "outbox"], ["engine", "outbox", "ack", "1", "--error", "x"]):
        out = runner.invoke(app, [*args, "--json"])
        assert out.exit_code == 1
        assert "paper engine" in json.loads(out.output)["message"]


def test_engine_fills_notify_splits_channel_and_alerts(
    cli_env: FakeProvider, monkeypatch, tmp_path: Path, _no_telegram
):
    intent = _cli_live(cli_env, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    snapshot = tmp_path / "fills.json"
    unmatched_sell = _order(intent, "AAA", side="sell", order_id="ord-2")
    snapshot.write_text(json.dumps({"orders": [_order(intent, "AAA", price="100.40")]}))

    out = runner.invoke(app, ["engine", "fills", str(snapshot), "--notify"])
    assert out.exit_code == 0, out.output
    (fill,) = _no_telegram
    assert fill["chat_id"] == "-100"
    assert fill["text"] == "[💵 live] filled BUY AAA 1.5 @ 100.40 (scan saw 288.10, -65.15%)"

    _no_telegram.clear()
    snapshot.write_text(json.dumps({"orders": [unmatched_sell]}))
    conflict = runner.invoke(app, ["engine", "fills", str(snapshot), "--notify"])
    assert conflict.exit_code == 1
    (alert,) = _no_telegram
    assert alert["chat_id"] == "555" and "CONFLICT" in alert["text"]


def test_notify_without_telegram_config_only_warns(
    cli_env: FakeProvider, monkeypatch, tmp_path: Path, _no_telegram
):
    intent = _cli_live(cli_env, monkeypatch)
    snapshot = tmp_path / "fills.json"
    snapshot.write_text(json.dumps({"orders": [_order(intent, "AAA", price="100.40")]}))
    out = runner.invoke(app, ["engine", "fills", str(snapshot), "--notify"])
    assert out.exit_code == 0
    assert "warning: --notify set" in out.output
    assert _no_telegram == []
