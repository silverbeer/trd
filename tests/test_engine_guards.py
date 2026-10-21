"""Live engine phase 2 (SB-1377): the guardrails before approvals go off.

A kill switch and four hard caps, enforced by trd independently of the strategy
config — at the scan (no intent that breaks one is written) and again at the
outbox (no spec that breaks one NOW is handed over). Plus the drift alarms that
say when the loop between trd and the broker has stopped turning: stale specs,
stuck intents, a cash ledger that disagrees with the broker, a loss stop that
tripped. These tests pin each guard firing, the paper engine being refused, the
daily report never summing real money with paper, and the good-faith-violation
property of the settled-cash ledger. Telegram is stubbed at urlopen; the clock is
passed in; FakeProvider supplies every quote.
"""

import gc
import json
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

import trd.cli.app as cli
from trd.cli.app import app
from trd.db.connection import connect
from trd.engine.settlement import unsettled_funding
from trd.errors import TrdError
from trd.models import (
    BrokerSnapshot,
    EngineMode,
    HaltCause,
    IntentStatus,
    OrderIntent,
    Side,
)
from trd.models.engine import STALE_INTENT_MINUTES
from trd.notify.messages import daily_report_message
from trd.notify.telegram import ALERT_REFUSED, LIVE_LABEL, alert_notifier_from_env
from trd.repos import InstrumentRepo, OrderIntentRepo, PriceRepo, TransactionRepo
from trd.repos.engine import EnginePositionRepo
from trd.services import EngineService, FillIngestService, OutboxService, ReconcileService, watchdog
from trd.services.backup import export_data, restore_data
from trd.services.daily_report import DailyReport, EntryLeg, engine_day
from trd.services.engine import ScanFill, ScanResult
from trd.services.guard import GuardService, stuck_intents

from .conftest import FakeProvider
from .test_daily_report import TODAY, day, event
from .test_engine_fills import _order, _snap
from .test_engine_live import MONDAY, _filled_position, _live, _names
from .test_engine_outbox import SESSION, _live_engine, _no_telegram, _row, _write

__all__ = ["_no_telegram"]  # autouse: no inherited Telegram config, sends captured

runner = CliRunner()
D = Decimal

SOON = MONDAY + timedelta(minutes=5)


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _buys(result: ScanResult) -> list[OrderIntent]:
    return [row.intent for row in result.intents if row.intent.side == Side.BUY]


def _sells(result: ScanResult) -> list[OrderIntent]:
    return [row.intent for row in result.intents if row.intent.side == Side.SELL]


# ---------------------------------------------------------------- paper refuses


def test_a_paper_engine_refuses_halt_resume_and_guard(engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"])
    for call in (
        lambda: engine.halt("x"),
        engine.resume,
        engine.guard,
        lambda: engine.set_guard(max_order=D("10")),
    ):
        with pytest.raises(TrdError, match="paper engine"):
            call()
    assert engine.status().guard is None and engine.status().halted is False


def test_live_init_writes_the_default_caps(engine, provider):
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"])  # size 5000, capital 100000, max 5
    guard = engine.guard()
    assert guard.stored
    assert guard.max_order == D("6250")  # size x 1.25
    assert guard.max_daily == D("100000")  # capital
    assert guard.max_positions == 5
    assert guard.max_loss == D("20000")  # 20% of capital
    assert guard.halted is False


def test_a_cap_cannot_be_switched_off(engine, provider):
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"])
    with pytest.raises(TrdError, match="must be positive"):
        engine.set_guard(max_order=D("0"))


# ------------------------------------------------------------------ kill switch


def test_halt_blocks_buys_empties_the_outbox_and_still_records_sells(engine, provider, conn):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"])
    _filled_position(engine, conn)  # AAA, stop 190
    provider.add_symbol("AAA", price="185")
    engine.halt("broker maintenance")

    halted = engine.scan(at=MONDAY)
    assert halted.halted and halted.halt_reason == "broker maintenance"
    assert _buys(halted) == []
    (sell,) = _sells(halted)  # the exit is still decided and recorded
    assert sell.rule == "stop"
    assert any(s.startswith("HALTED — no buy intents") for s in halted.skipped), halted.skipped
    # The passed-over signal is still in the log, unacted.
    assert [s.symbol for s in halted.signals if not s.acted] == ["BBB"]

    status = engine.status()
    assert status.halted and status.halt_reason == "broker maintenance"
    assert status.halted_by == HaltCause.MANUAL and status.halted_at is not None

    outbox = OutboxService(conn).pending(SOON)
    assert outbox.halted and outbox.halt_reason == "broker maintenance"
    assert outbox.orders == [] and outbox.withheld == []

    engine.resume()
    resumed = OutboxService(conn).pending(SOON)
    assert not resumed.halted
    assert [o.side for o in resumed.orders] == [Side.SELL]
    later = engine.scan(at=MONDAY + timedelta(hours=1))
    assert [row.instrument.symbol for row in later.intents] == ["BBB"]
    assert not later.halted


def test_a_halt_survives_a_capital_change(engine, provider):
    """set_capital rewrites the config row; the guard hangs off the account."""
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"])
    engine.set_guard(max_order=D("777"))
    engine.halt("stop")
    engine.set_capital(D("200000"))
    guard = engine.guard()
    assert guard.halted and guard.halt_reason == "stop"
    assert guard.max_order == D("777")


def test_a_reinit_does_not_release_the_kill_switch(engine, provider):
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"])
    engine.halt("stop")
    _live(engine, ["AAA"])
    assert engine.guard().halted


# ------------------------------------------------------------------- hard caps


def test_max_order_blocks_at_the_scan(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    engine.set_guard(max_order=D("1000"))
    result = engine.scan(at=MONDAY)
    assert result.intents == []
    assert any("hard cap" in s and "max-order $1,000.00" in s for s in result.skipped)
    assert all(not s.acted for s in result.signals)
    assert OrderIntentRepo(conn).list_for(engine.account().id) == []


def test_lowering_a_cap_withholds_an_intent_already_written(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    (intent,) = _buys(engine.scan(at=MONDAY))
    assert [o.intent_id for o in OutboxService(conn).pending(SOON).orders] == [intent.id]

    engine.set_guard(max_order=D("1000"))
    outbox = OutboxService(conn).pending(SOON)
    assert outbox.orders == []
    (held,) = outbox.withheld
    assert held.order.intent_id == intent.id and not held.stale
    assert "max-order" in held.reason


def test_max_daily_counts_todays_buys_but_not_rejected_ones(engine, provider, conn):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], max_positions=3)
    engine.set_guard(max_daily=D("7000"))  # room for one ~$5,000 buy, not two

    first = engine.scan(at=MONDAY)
    (taken,) = _buys(first)
    assert any("max-daily $7,000.00" in s for s in first.skipped), first.skipped

    # Rejected bought nothing, so it frees the day's dollars for the other name.
    OrderIntentRepo(conn).update(taken.id, status=IntentStatus.REJECTED)
    again = engine.scan(at=MONDAY + timedelta(hours=1))
    (second,) = _buys(again)
    assert second.instrument_id != taken.instrument_id


def test_lowering_max_daily_withholds_the_later_of_two_pending_buys(engine, provider, conn):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], max_positions=3)
    first, second = _buys(engine.scan(at=MONDAY))
    engine.set_guard(max_daily=D("7000"))
    outbox = OutboxService(conn).pending(SOON)
    assert [o.intent_id for o in outbox.orders] == [first.id]
    assert [w.order.intent_id for w in outbox.withheld] == [second.id]
    assert "max-daily" in outbox.withheld[0].reason


def test_max_positions_counts_positions_and_open_buys(engine, provider, conn):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], max_positions=5)
    engine.set_guard(max_positions=1)
    result = engine.scan(at=MONDAY)
    assert len(_buys(result)) == 1
    assert any("max-positions is 1" in s for s in result.skipped), result.skipped


def test_caps_never_withhold_a_sell(engine, provider, conn):
    account_id = _live_engine(engine, conn, provider)
    sell = _write(conn, account_id, SESSION, side=Side.SELL, quantity=D("100"))
    engine.set_guard(max_order=D("1"), max_daily=D("1"), max_positions=1)
    outbox = OutboxService(conn).pending(SESSION + timedelta(minutes=5))
    assert [o.intent_id for o in outbox.orders] == [sell.id]


# -------------------------------------------------------------------- loss stop


def test_the_loss_stop_halts_the_engine_from_the_scan(engine, provider, conn, _no_telegram):
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"])
    _filled_position(engine, conn)  # 10 @ 200
    engine.set_guard(max_loss=D("100"))
    provider.add_symbol("AAA", price="185")  # -150 unrealized

    result = engine.scan(at=MONDAY)
    assert result.auto_halt is not None and "loss stop" in result.auto_halt
    assert result.halted
    assert _buys(result) == [] and len(_sells(result)) == 1
    guard = engine.guard()
    assert guard.halted and guard.halted_by == HaltCause.MAX_LOSS and guard.auto_halted
    assert engine.status().halted_by == HaltCause.MAX_LOSS

    # Alerted once: the pass that tripped it says so, the next one does not.
    again = engine.scan(at=MONDAY + timedelta(hours=1))
    assert again.auto_halt is None and again.halted


def test_a_loss_halt_is_sent_to_the_alert_chat(monkeypatch, _no_telegram):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    result = ScanResult(run_id=1, at=MONDAY, paper=True, scanned=1, auto_halt="loss stop: x")
    cli._notify_scan(result, LIVE_LABEL)
    assert [m["chat_id"] for m in _no_telegram] == ["555"]
    assert "HALTED — the loss stop tripped" in _no_telegram[0]["text"]


def test_the_loss_stop_halts_the_engine_from_fill_ingest(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    (intent,) = _buys(engine.scan(at=MONDAY))
    engine.set_guard(max_loss=D("1"))
    instrument = InstrumentRepo(conn).get_by_symbol("AAA")
    assert instrument is not None
    close = PriceRepo(conn).latest_close(instrument.id)
    assert close is not None
    # Filled 10% above the stored close: marked there, the book is down ~$500.
    fill = f"{(close * D('1.1')).quantize(D('0.01'))}"
    result = FillIngestService(conn).ingest(
        _snap([_order(intent, "AAA", price=fill)]), MONDAY + timedelta(hours=1)
    )
    assert result.auto_halt is not None and "loss stop" in result.auto_halt
    assert engine.guard().halted_by == HaltCause.MAX_LOSS


# ----------------------------------------------------------------- stale specs


def test_a_stale_buy_is_withheld_unless_asked_for(engine, provider, conn):
    account_id = _live_engine(engine, conn, provider)
    buy = _write(conn, account_id, SESSION)
    sell = _write(conn, account_id, SESSION, side=Side.SELL)
    late = SESSION + timedelta(minutes=STALE_INTENT_MINUTES + 15)

    outbox = OutboxService(conn).pending(late)
    # A sell's size is the position, not a quote: never withheld as stale.
    assert [o.intent_id for o in outbox.orders] == [sell.id]
    (held,) = outbox.withheld
    assert held.stale and held.order.intent_id == buy.id
    assert held.reason == "stale — sized off a quote from 10:00"

    listed = OutboxService(conn).pending(late, include_stale=True)
    assert [o.intent_id for o in listed.orders] == [buy.id, sell.id]
    assert listed.orders[0].note == "stale — sized off a quote from 10:00"
    assert listed.withheld == []

    fresh = OutboxService(conn).pending(SESSION + timedelta(minutes=STALE_INTENT_MINUTES))
    assert len(fresh.orders) == 2  # exactly at the limit is not stale


def test_the_repo_expires_only_stale_pending_buys(engine, provider, conn):
    """What the live scan sweeps first: a buy the outbox would withhold as stale
    stops holding its name and cash, so the same pass can re-plan it."""
    account_id = _live_engine(engine, conn, provider)
    stale_buy = _write(conn, account_id, SESSION)
    sell = _write(conn, account_id, SESSION, side=Side.SELL)
    repo = OrderIntentRepo(conn)
    cutoff = SESSION + timedelta(minutes=1)
    assert [i.id for i, _ in repo.expire_pending_buys_before(account_id, cutoff, cutoff)] == [
        stale_buy.id
    ]
    expired, kept = repo.get(stale_buy.id), repo.get(sell.id)
    assert expired is not None and expired.status == IntentStatus.EXPIRED
    assert kept is not None and kept.status == IntentStatus.PENDING
    assert repo.expire_pending_buys_before(account_id, cutoff, cutoff) == []


# ------------------------------------------------------------- reconcile cash


def _snapshot(cash: str | None, unsettled: str | None = None) -> BrokerSnapshot:
    doc: dict = {"as_of": "2026-10-21T10:00:00", "positions": []}
    if cash is not None:
        doc["cash"] = cash
    if unsettled is not None:
        doc["unsettled"] = unsettled
    return BrokerSnapshot.model_validate(doc)


def test_cash_is_compared_only_when_asked(engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"], capital=D("1000"))
    service = ReconcileService(conn)
    ledger = engine.cash()
    assert ledger is not None

    # A broker account holding more than the engine's capital is not a mismatch
    # unless the caller says the cash is the engine's alone.
    surplus = service.reconcile(_snapshot("3000"), "brokerage")
    assert surplus.cash is None and surplus.in_sync

    close = service.reconcile(_snapshot("1000.04", "0"), "brokerage", cash=ledger)
    assert close.cash is not None and close.cash.matched and close.in_sync

    off = service.reconcile(_snapshot("1000.10"), "brokerage", cash=ledger)
    assert off.cash_mismatch and not off.in_sync
    assert off.cash is not None and off.cash.settled_delta == D("0.10")

    unsettled = service.reconcile(_snapshot("1000", "5"), "brokerage", cash=ledger)
    assert unsettled.cash_mismatch

    with pytest.raises(TrdError, match="needs the broker's cash"):
        service.reconcile(_snapshot(None), "brokerage", cash=ledger)


def _cli_live(cli_env: FakeProvider) -> None:
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


def test_cli_reconcile_compare_cash_exits_on_a_mismatch(cli_env: FakeProvider, tmp_path: Path):
    _cli_live(cli_env)
    snap = tmp_path / "broker.json"
    snap.write_text(json.dumps({"as_of": "2026-10-21T10:00:00", "cash": "4900", "positions": []}))

    plain = runner.invoke(app, ["engine", "reconcile", str(snap)])
    assert plain.exit_code == 0, plain.output
    compared = runner.invoke(app, ["engine", "reconcile", str(snap), "--compare-cash"])
    assert compared.exit_code == 1
    assert "CASH MISMATCH" in compared.output

    snap.write_text(json.dumps({"as_of": "2026-10-21T10:00:00", "cash": "5000", "positions": []}))
    doc = runner.invoke(app, ["engine", "reconcile", str(snap), "--compare-cash", "--json"])
    assert doc.exit_code == 0, doc.output
    parsed = json.loads(doc.output)
    assert parsed["cash"]["matched"] is True and parsed["cash_mismatch"] is False


# --------------------------------------------------------------- stuck intents


def _intent(status: IntentStatus, created: datetime, submitted: datetime | None = None):
    return OrderIntent(
        id=1,
        account_id=1,
        instrument_id=1,
        side=Side.BUY,
        quantity=D("1"),
        ref_price=D("100"),
        notional=D("100"),
        reason="r",
        strategy="momentum",
        ref_id="ref",
        status=status,
        created_at=created,
        submitted_at=submitted,
    )


def test_stuck_means_submitted_past_its_session_or_an_approval_past_expiry():
    tue = datetime(2026, 10, 20, 10, 0)
    mon = datetime(2026, 10, 19, 10, 0)
    assert stuck_intents([_intent(IntentStatus.SUBMITTED, mon, mon)], tue) == 1
    assert stuck_intents([_intent(IntentStatus.SUBMITTED, tue, tue)], tue) == 0
    assert stuck_intents([_intent(IntentStatus.AWAITING_APPROVAL, mon)], tue) == 1
    morning = datetime(2026, 10, 20, 9, 50)
    assert stuck_intents([_intent(IntentStatus.AWAITING_APPROVAL, morning)], tue) == 0
    assert (
        stuck_intents(
            [_intent(IntentStatus.AWAITING_APPROVAL, morning)], tue.replace(hour=16, minute=5)
        )
        == 1
    )
    # Unplaced: the expiry sweep's business, not a stuck order.
    assert stuck_intents([_intent(IntentStatus.PENDING, mon)], tue) == 0


def test_status_counts_and_publishes_stuck_intents(engine, provider, conn):
    account_id = _live_engine(engine, conn, provider)
    old = datetime(2024, 9, 16, 10, 0)
    _write(conn, account_id, old, status=IntentStatus.SUBMITTED)
    _write(conn, account_id, old, symbol="BBB", status=IntentStatus.AWAITING_APPROVAL)
    status = engine.status()
    assert status.stuck_intents == 2
    assert status.awaiting_approval == 1
    published = json.loads(status.model_dump_json())
    assert published["stuck_intents"] == 2 and published["halted"] is False


def _home(tmp_path: Path, name: str, at: datetime, **status) -> Path:
    home = tmp_path / name
    home.mkdir(exist_ok=True)
    payload = {"last_scan": (at - timedelta(minutes=2)).isoformat(), **status}
    (home / watchdog.STATUS_FILENAME).write_text(json.dumps(payload))
    return home


def test_the_watchdog_alerts_once_on_stuck_intents_and_clears(tmp_path: Path):
    at = datetime(2026, 10, 20, 11, 0)  # a Tuesday, in session
    home = _home(tmp_path, "live", at, stuck_intents=1)
    result = watchdog.check([("live", home)], now=at)
    assert not result.healthy and result.scanning and result.failing == ["live"]

    message, state = watchdog.decide(result, watchdog.WatchdogState())
    assert message is not None
    assert message.startswith("🟠 trd live engine needs a person")
    assert "1 stuck order intent(s)" in message
    assert state.alerting and state.kind == "guard"

    later = watchdog.check([("live", home)], now=at + timedelta(minutes=10))
    repeat, state = watchdog.decide(later, state)
    assert repeat is None  # deduped: the same alarm is not news ten minutes on

    _home(tmp_path, "live", at + timedelta(minutes=20), stuck_intents=0)
    cleared = watchdog.check([("live", home)], now=at + timedelta(minutes=20))
    recovery, state = watchdog.decide(cleared, state)
    assert recovery is not None and recovery.startswith("🟢 trd live engine alarms cleared")
    assert not state.alerting


def test_the_watchdog_alarms_on_a_loss_halt_but_not_a_manual_one(tmp_path: Path):
    at = datetime(2026, 10, 20, 11, 0)
    auto = _home(
        tmp_path, "auto", at, halted=True, halted_by="max_loss", halt_reason="loss stop: -25"
    )
    manual = _home(tmp_path, "manual", at, halted=True, halted_by="manual", halt_reason="lunch")
    result = watchdog.check([("auto", auto), ("manual", manual)], now=at)
    assert result.failing == ["auto"]
    message, _ = watchdog.decide(result, watchdog.WatchdogState())
    assert message is not None and "HALTED by its loss stop — loss stop: -25" in message


def test_a_stopped_engine_still_leads_with_not_scanning(tmp_path: Path):
    """The alarms ride the existing alert; they do not replace its heading."""
    at = datetime(2026, 10, 20, 11, 0)
    home = tmp_path / "live"
    home.mkdir()
    payload = {"last_scan": (at - timedelta(hours=2)).isoformat(), "stuck_intents": 2}
    (home / watchdog.STATUS_FILENAME).write_text(json.dumps(payload))
    message, state = watchdog.decide(
        watchdog.check([("live", home)], now=at), watchdog.WatchdogState()
    )
    assert message is not None and message.startswith("🔴 trd engine is not scanning")
    assert "2 stuck order intent(s)" in message and state.kind == "scan"


# ------------------------------------------------------------- alert fallback


def test_a_live_call_to_action_never_falls_back_to_the_channel(monkeypatch, _no_telegram):
    env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "-100"}
    errors: list[str] = []
    assert alert_notifier_from_env(env, error=errors.append) is None
    assert errors == [ALERT_REFUSED]

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    intents = ScanResult(run_id=1, at=MONDAY, paper=True, scanned=1, intents=[_row()])
    cli._notify_scan(intents, LIVE_LABEL)
    assert _no_telegram == []  # refused, not posted publicly

    # A fill report is not a call to action: the channel is still its home.
    fill = ScanFill(symbol="AAA", strategy="momentum", quantity=D("1"), price=D("10"), reason="r")
    filled = ScanResult(run_id=2, at=MONDAY, paper=True, scanned=1, opened=[fill])
    cli._notify_scan(filled, "swing")
    assert [m["chat_id"] for m in _no_telegram] == ["-100"]


# ------------------------------------------------------- good-faith violations


def _fill_at(order: dict, local: datetime) -> dict:
    """Move an order's execution to `local` (New York, EDT in these dates)."""
    stamp = (local + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for execution in order["executions"]:
        execution["timestamp"] = stamp
    order["last_transaction_at"] = stamp
    return order


def test_every_live_buy_is_funded_from_settled_cash(engine, provider, conn):
    """The ledger's GFV guarantee, end to end: buy, stop out, and the refill has
    to wait for T+1 — so no position is ever bought with proceeds that have not
    settled, and selling one the same day it was bought cannot be a GFV."""
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], capital=D("8000"), max_positions=1)
    engine.set_guard(max_loss=D("8000"))  # this test halves a position on purpose
    fills = FillIngestService(conn)

    (first,) = _buys(engine.scan(at=MONDAY))
    bought_first = InstrumentRepo(conn).get(first.instrument_id)
    assert bought_first is not None
    symbol = bought_first.symbol
    fills.ingest(_snap([_order(first, symbol, price=f"{first.ref_price:.2f}")]), MONDAY)

    provider.add_symbol(symbol, price=f"{first.ref_price / 2:.2f}")  # through any stop
    exit_scan = engine.scan(at=MONDAY + timedelta(hours=1))
    (sell,) = _sells(exit_scan)
    sold = _fill_at(
        _order(sell, symbol, price=f"{sell.ref_price:.2f}", order_id="ord-2"),
        MONDAY + timedelta(hours=1, minutes=5),
    )
    fills.ingest(_snap([sold]), MONDAY + timedelta(hours=1, minutes=10))

    # The slot is free, the proceeds are not: a same-day refill is refused.
    refill = engine.scan(at=MONDAY + timedelta(hours=2))
    assert _buys(refill) == []
    assert any("settles 2024-09-17" in s for s in refill.skipped), refill.skipped

    # Next session the proceeds have settled and the refill goes ahead.
    tuesday = MONDAY + timedelta(days=1)
    for name in ("AAA", "BBB"):
        instrument = InstrumentRepo(conn).get_by_symbol(name)
        assert instrument is not None
        close = PriceRepo(conn).latest_close(instrument.id)
        assert close is not None
        provider.add_symbol(name, price=f"{close * D('1.002'):.2f}")
    (second,) = _buys(engine.scan(at=tuesday))
    other = InstrumentRepo(conn).get(second.instrument_id)
    assert other is not None
    bought = _fill_at(
        _order(second, other.symbol, price=f"{second.ref_price:.2f}", order_id="ord-3"),
        tuesday + timedelta(minutes=30),
    )
    fills.ingest(_snap([bought]), tuesday + timedelta(minutes=40))

    txns = TransactionRepo(conn).list_chronological(engine.account().id)
    assert [t.side for t in txns] == [Side.BUY, Side.SELL, Side.BUY]
    prior = []
    for txn in txns:
        if txn.side == Side.BUY:
            cost = txn.quantity * txn.price + txn.fees
            assert unsettled_funding(D("8000"), prior, txn.executed_at.date(), cost) is None
        prior.append((txn.executed_at.date(), txn.side, txn.quantity, txn.price, txn.fees))


def test_a_sell_of_a_position_bought_on_unsettled_cash_is_flagged(engine, provider, conn):
    """Should never happen under the ledger — written directly to prove the
    defensive check in the live exit path says so when it does."""
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], capital=D("1000"))
    account = engine.account()
    txns = TransactionRepo(conn)
    aaa = InstrumentRepo(conn).get_by_symbol("AAA")
    bbb = InstrumentRepo(conn).get_by_symbol("BBB")
    assert aaa is not None and bbb is not None

    def trade(instrument_id: int, side: Side, price: str, minute: int) -> datetime:
        at = MONDAY.replace(hour=9, minute=minute)
        txns.insert(
            account_id=account.id,
            instrument_id=instrument_id,
            side=side,
            quantity=D("4"),
            price=D(price),
            fees=D(0),
            executed_at=at,
            note="direct",
        )
        return at

    trade(aaa.id, Side.BUY, "200", 40)  # 800 of the 1000
    trade(aaa.id, Side.SELL, "210", 50)  # 840 back, settles Tuesday
    opened = trade(bbb.id, Side.BUY, "200", 55)  # 800 with 200 settled: a GFV in waiting
    EnginePositionRepo(conn).open(
        account_id=account.id,
        instrument_id=bbb.id,
        signal_id=None,
        strategy="momentum",
        opened_at=opened,
        entry_price=D("200"),
        quantity=D("4"),
        stop_price=D("190"),
        target_price=D("220"),
        atr_at_entry=D("5"),
        last_bar_date=None,
    )
    provider.add_symbol("BBB", price="185")

    result = engine.scan(at=MONDAY)
    assert len(_sells(result)) == 1  # still recorded
    warning = [s for s in result.skipped if "GOOD-FAITH VIOLATION RISK" in s]
    assert warning and "settles 2024-09-17" in warning[0], result.skipped


# ---------------------------------------------------------------- daily report


def _live_day(name: str = "live", **update):
    return day(name).model_copy(
        update={
            "mode": EngineMode.LIVE,
            "capital": D("100"),
            "bankroll": None,
            "realized_all": D("3"),
            "unrealized": D("1.20"),
            "realized_today": D("2"),
            "cash_settled": D("80"),
            "cash_unsettled": D("20"),
            "open_intents": 1,
            "awaiting_approval": 1,
            **update,
        }
    )


def test_the_live_engine_leads_and_is_never_in_a_paper_total():
    report = DailyReport(on=TODAY, engines=[day("swing"), _live_day(), day("swing20")])
    lines = daily_report_message(report).splitlines()
    assert lines[2] == "💵 LIVE (real money) — live"
    block = lines[3:12]
    assert any(line.split()[:2] == ["capital", "100.00"] for line in block), block
    assert any(line.split()[:3] == ["worth", "now", "104.20"] for line in block), block
    assert any("+4.20" in line and "+4.2%" in line for line in block), block
    assert any("80.00 settled" in line and "20.00 unsettled" in line for line in block)
    assert any("1 open" in line and "1 awaiting approval" in line for line in block)
    assert any(line.split()[:2] == ["state", "trading"] for line in block)

    money = lines.index("MONEY (paper) — what went in, what it is worth")
    assert money > 2
    # Two paper engines: "both", summing paper only.
    assert report.net == D("160") and report.realized_today == D("0")
    assert any(line.split()[:2] == ["invested", "2,000.00"] for line in lines)
    both = [line for line in lines if line.split()[:1] == ["both"]]
    assert both[0].split()[1] == "+0.00"  # TODAY: the live +2.00 is not in it
    assert not any(line.split()[:1] == ["all"] for line in lines)
    # The live engine still has its own row in the per-engine sections.
    assert any(line.startswith("live ") for line in lines[money:])


def test_a_halted_live_engine_says_so_in_the_report():
    halted = _live_day(halted=True, halt_reason="loss stop: -25")
    text = daily_report_message(DailyReport(on=TODAY, engines=[halted, day("swing")]))
    assert any(
        line.split()[:2] == ["state", "HALTED"] and "loss stop: -25" in line
        for line in text.splitlines()
    )


def test_live_against_its_paper_twin():
    live = _live_day(
        entries=[EntryLeg(symbol="AAA", session=TODAY, price=D("101"))],
        exits_today=[event("AAA", "5", rule="target", r="1.0")],
    )
    twin = day("swing20").model_copy(
        update={
            "entries": [
                EntryLeg(symbol="AAA", session=TODAY, price=D("100")),
                EntryLeg(symbol="BBB", session=TODAY, price=D("50")),
            ],
            "exits_today": [event("AAA", "5", rule="target", r="1.0"), event("BBB", "-5")],
        }
    )
    report = DailyReport(on=TODAY, engines=[live, twin])
    (d,) = report.divergence
    assert d.shared_entries == 1 and d.avg_entry_gap_pct == D("1")
    text = daily_report_message(report)
    assert (
        "  vs swing20: closed today 1 live / 2 paper · entry +1.00% vs paper fill, "
        "avg of n=1 shared buy(s) (30d)"
    ) in text.splitlines()

    renamed = DailyReport(on=TODAY, engines=[live, twin], live_twin="other")
    assert renamed.divergence == [] and "vs swing20" not in daily_report_message(renamed)


GOLDEN_THREE_PAPER = (
    "📊 trd daily — Thu Sep 3\n⚠ day marks are stale — priced at an older session, not today\n\n"
    "MONEY — what went in, what it is worth\n"
    "  invested       4,000.00  what all engines may put to work at once\n"
    "  worth now      4,240.00\n"
    "  return          +240.00  +6.0%\n"
    "    cash booked   +300.00  out of trades that are done\n"
    "    still open     -60.00  on paper, not sold yet\n"
    "  swing    1,000.00 → 1,080.00   +80.00  +8.0%\n"
    "  day      1,000.00 → 1,080.00   +80.00  +8.0%\n"
    "  swing20  2,000.00 → 2,080.00   +80.00  +4.0%\n\n"
    "TODAY — trades that closed on this session\n"
    "swing    -15.00 · 2 exits\nday      +5.00 · 1 exit\nswing20  +0.00 · no exits\n"
    "all      -10.00 · 3 exits\n\n"
    "WORKING (30d) — best strategy by expectancy, the average R a trade of that kind returns\n"
    "swing    not enough closed trades to say\nday      not enough closed trades to say\n"
    "swing20  not enough closed trades to say\n\n"
    "NOT WORKING (30d)\nno strategy has enough closed trades to blame yet\n"
    "today's losses by exit\n  Stop Loss x1 -40.00\n\n"
    "TODAY'S TRADES\nswing\n"
    "  AAA +25.00 (+2.50R) · 100.00 → 125.00 · 3 sessions · Profit Target\n"
    "  BBB -40.00 (-1.00R) · 100.00 → 90.00 · 3 sessions · Stop Loss\n"
    "day\n  CCC +5.00 (+0.50R) · 100.00 → 90.00 · 49m · Session Close\n\n"
    "OPEN BOOK (now)\n"
    "swing    2 open · unrealized -20.00 · at risk 50.00\n"
    "day      2 open · unrealized -20.00 · at risk 50.00\n"
    "swing20  2 open · unrealized -20.00 · at risk 50.00\n"
    "all      at risk 150.00\n"
    "at risk = what the open book loses if every stop hits · "
    "1R = what one trade had at risk between its entry and its stop"
)

GOLDEN_RISK_SIZED = (
    "📊 trd daily — Thu Sep 3\n\n"
    "MONEY — what went in, what it is worth\n"
    "  invested             —  this engine sizes by risk, so there is no fixed amount in\n"
    "  made so far     +80.00\n"
    "    cash booked  +100.00  out of trades that are done\n"
    "    still open    -20.00  on paper, not sold yet\n\n"
    "TODAY — trades that closed on this session\nswing  +0.00 · no exits\n\n"
    "WORKING (30d) — best strategy by expectancy, the average R a trade of that kind returns\n"
    "swing  not enough closed trades to say\n\n"
    "NOT WORKING (30d)\nno strategy has enough closed trades to blame yet\n\n"
    "OPEN BOOK (now)\nswing  2 open · unrealized -20.00 · at risk 50.00\n"
    "at risk = what the open book loses if every stop hits · "
    "1R = what one trade had at risk between its entry and its stop"
)


def test_with_no_live_engine_the_message_is_byte_identical():
    """Captured from the build before SB-1377: paper-only reports must not move."""
    three = DailyReport(
        on=TODAY,
        engines=[
            day(
                "swing",
                realized_today=D("-15"),
                exits_today=[
                    event("AAA", "25", rule="target", r="2.5", exit_="125"),
                    event("BBB", "-40"),
                ],
            ),
            day(
                "day",
                timeframe="5m",
                realized_today=D("5"),
                exits_today=[event("CCC", "5", rule="session_close", r="0.5", minutes=49)],
                marks_are_stale=True,
            ),
            day("swing20", bankroll=D("2000")),
        ],
    )
    assert daily_report_message(three) == GOLDEN_THREE_PAPER
    single = DailyReport(on=TODAY, engines=[day("swing", bankroll=None)])
    assert daily_report_message(single) == GOLDEN_RISK_SIZED


def test_engine_day_reads_a_live_engines_money(engine, provider, conn):
    _names(conn, provider, "AAA")
    _live(engine, ["AAA"])
    (intent,) = _buys(engine.scan(at=MONDAY))
    reduced = engine_day(engine, "live", MONDAY.date())
    assert reduced.is_live and reduced.capital == D("100000")
    assert reduced.open_intents == 1 and reduced.awaiting_approval == 0
    assert reduced.cash_settled == D("100000") - intent.notional
    assert reduced.halted is False


# ----------------------------------------------------------------------- backup


def test_backup_round_trips_the_halt_and_the_caps(tmp_path: Path, engine, provider, conn):
    provider.add_symbol("AAA", price="100")
    _live(engine, ["AAA"])
    engine.set_guard(max_order=D("123.45"), max_loss=D("50"))
    engine.halt("maintenance")
    data = export_data(conn)
    (exported,) = data["engine"]["guards"]
    assert exported["account"] == "brokerage" and exported["halted"] is True

    fresh = connect(tmp_path / "restored.duckdb")
    restore_data(fresh, data)
    restored = EngineService(fresh, provider)
    guard = GuardService(fresh).guard(restored.config())
    assert guard is not None and guard.stored
    assert guard.halted and guard.halt_reason == "maintenance"
    assert guard.halted_by == HaltCause.MANUAL and guard.halted_at is not None
    assert guard.max_order == D("123.45") and guard.max_loss == D("50")
    assert guard.max_daily == D("100000") and guard.max_positions == 5


# -------------------------------------------------------------------------- CLI


def test_cli_halt_guard_status_outbox_resume(cli_env: FakeProvider):
    _cli_live(cli_env)

    shown = runner.invoke(app, ["engine", "guard", "--json"])
    assert shown.exit_code == 0, shown.output
    assert D(json.loads(shown.output)["max_order"]) == D("1250")  # size 1000 x 1.25

    lowered = runner.invoke(app, ["engine", "guard", "--max-order", "900", "--json"])
    assert lowered.exit_code == 0 and D(json.loads(lowered.output)["max_order"]) == D("900")
    assert runner.invoke(app, ["engine", "guard", "--max-order", "0"]).exit_code == 1

    halted = runner.invoke(app, ["engine", "halt", "--reason", "going to lunch", "--json"])
    assert halted.exit_code == 0, halted.output
    assert json.loads(halted.output)["halted"] is True

    status = json.loads(runner.invoke(app, ["engine", "status", "--json"]).output)
    assert status["halted"] is True and status["halt_reason"] == "going to lunch"
    assert status["halted_at"] is not None and status["halted_by"] == "manual"
    assert status["stuck_intents"] == 0

    # The commands above leave their read-write connections to the garbage
    # collector; DuckDB refuses a read-only open (outbox) beside a live one in the
    # same process. A real shell runs each command in its own process.
    gc.collect()
    raw = runner.invoke(app, ["engine", "outbox", "--json"])
    assert raw.exit_code == 0, (raw.output, raw.exception)
    outbox = json.loads(raw.output)
    assert outbox["halted"] is True and outbox["orders"] == []
    gc.collect()
    human = runner.invoke(app, ["engine", "outbox"])
    assert human.exit_code == 0 and "HALTED" in human.output

    assert runner.invoke(app, ["engine", "resume"]).exit_code == 0
    after = json.loads(runner.invoke(app, ["engine", "status", "--json"]).output)
    assert after["halted"] is False and after["halt_reason"] is None


def test_cli_halt_is_refused_on_a_paper_engine(cli_env: FakeProvider):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert runner.invoke(app, ["engine", "init", "--symbols", "AAA"]).exit_code == 0
    refused = runner.invoke(app, ["engine", "halt", "--json"])
    assert refused.exit_code == 1
    assert json.loads(refused.output)["error"]
