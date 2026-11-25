"""A live exit nobody acts on is asked about until it is (SB-1735).

On 2026-11-17 a stop-out sat waiting on a trade approval for six hours, its push
never arrived, and it lapsed at 16:00 with nobody told. These pin each layer that
now speaks up: the runner's reminder (`engine nudge`), the watchdog's alarm, the
scan's expiry alert, and the ack saying whether its message went out.

Synthetic data only: invented symbols and prices.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from typer.testing import CliRunner

import trd.cli.app as cli
from trd.cli.app import app
from trd.config import get_settings
from trd.db.connection import connect
from trd.models import (
    Instrument,
    InstrumentType,
    IntentStatus,
    OrderIntent,
    OrderIntentRow,
    Side,
    WaitingExit,
)
from trd.notify.messages import exit_waiting_message, expired_exit_messages
from trd.repos import AccountRepo, OrderIntentRepo
from trd.services.engine import ScanResult
from trd.services.exit_nudge import (
    due_nudges,
    in_session,
    load_state,
    minutes_waiting,
    save_state,
)
from trd.services.guard import waiting_exits
from trd.services.watchdog import EXIT_WAIT_ALARM_MINUTES, exit_wait_alarms, guard_alarms

from .conftest import FakeProvider
from .test_engine_live import MONDAY
from .test_engine_outbox import (
    TELEGRAM_ENV,
    _cli_live,
    _Frozen,
    _live_engine,
    _write,
    engine,  # noqa: F401 — fixture
)

runner = CliRunner()

# A Wednesday, in the session.
WED = datetime(2026, 10, 21, 10, 5)


@pytest.fixture(autouse=True)
def _sent(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
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


def _telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")


def _intent(
    side: Side = Side.SELL,
    status: IntentStatus = IntentStatus.AWAITING_APPROVAL,
    created: datetime = WED - timedelta(minutes=40),
    submitted: datetime | None = None,
    intent_id: int = 1,
    approval_id: str | None = "appr-1",
) -> OrderIntent:
    return OrderIntent(
        id=intent_id,
        account_id=1,
        instrument_id=1,
        side=side,
        quantity=D("0.25"),
        ref_price=D("48.20"),
        notional=D("12.05"),
        reason="hit the stop at 49.00 — thesis broke, lost 1R (2.00/share)",
        rule="stop" if side == Side.SELL else None,
        strategy="pullback",
        ref_id=f"ref-{intent_id}",
        status=status,
        approval_id=approval_id,
        created_at=created,
        submitted_at=submitted,
    )


XYZ = Instrument(id=1, symbol="XYZ", type=InstrumentType.STOCK)


def _waiting(since: datetime, status: IntentStatus = IntentStatus.AWAITING_APPROVAL, **kw):
    return WaitingExit(
        intent_id=kw.get("intent_id", 1),
        symbol="XYZ",
        rule="stop",
        status=status,
        since=since,
        ref_price=D("48.20"),
        reason="hit the stop at 49.00",
    )


# ------------------------------------------------------------- what is waiting


def test_only_sells_not_yet_at_the_broker_are_waiting():
    rows = [
        (_intent(intent_id=1, status=IntentStatus.AWAITING_APPROVAL), XYZ),
        (_intent(intent_id=2, status=IntentStatus.PENDING, approval_id=None), XYZ),
        (_intent(intent_id=3, status=IntentStatus.SUBMITTED), XYZ),  # the broker has an order
        (_intent(intent_id=4, side=Side.BUY), XYZ),  # a buy waiting costs an opportunity only
    ]
    assert [w.intent_id for w in waiting_exits(rows)] == [1, 2]


def test_a_placed_exit_waits_from_when_it_reached_the_broker():
    placed = WED - timedelta(minutes=20)
    [waiting] = waiting_exits(
        [(_intent(created=WED - timedelta(minutes=50), submitted=placed), XYZ)]
    )
    assert waiting.since == placed
    assert minutes_waiting(waiting, WED) == 20


def test_an_exit_decided_before_the_bell_waits_from_the_open():
    pre_market = _waiting(datetime(2026, 10, 21, 7, 41), IntentStatus.PENDING)
    assert minutes_waiting(pre_market, datetime(2026, 10, 21, 9, 40)) == 10


# ---------------------------------------------------------------- the reminder


def test_first_reminder_after_fifteen_minutes():
    assert due_nudges([_waiting(WED - timedelta(minutes=14))], WED, {}) == []
    [due] = due_nudges([_waiting(WED - timedelta(minutes=15))], WED, {})
    assert due.minutes == 15


def test_reminders_repeat_every_thirty_minutes_while_it_waits():
    waiting = [_waiting(WED - timedelta(minutes=60))]
    assert due_nudges(waiting, WED, {1: WED - timedelta(minutes=29)}) == []
    assert len(due_nudges(waiting, WED, {1: WED - timedelta(minutes=30)})) == 1


@pytest.mark.parametrize(
    "at",
    [
        datetime(2026, 10, 21, 8, 0),  # before the open
        datetime(2026, 10, 21, 16, 30),  # after the close
        datetime(2026, 10, 24, 11, 0),  # a Saturday
    ],
)
def test_no_reminder_outside_the_session(at):
    assert due_nudges([_waiting(at - timedelta(hours=3))], at, {}) == []


def test_state_round_trips_and_forgets_answered_exits(tmp_path: Path):
    path = tmp_path / ".exit-nudges.json"
    save_state(path, {1: WED, 2: WED}, still_waiting={1})
    assert load_state(path) == {1: WED}
    path.write_text("not json")
    assert load_state(path) == {}


def test_the_reminder_says_where_it_is_stuck():
    asked = exit_waiting_message(_waiting(WED), 45)
    assert "STOP LOSS EXIT waiting 45 min" in asked and "XYZ" in asked
    assert "YOUR approval" in asked and "16:00" in asked
    unplaced = exit_waiting_message(_waiting(WED, IntentStatus.PENDING), 20)
    assert "not placed yet" in unplaced and "live runner" in unplaced


def test_an_early_close_ends_the_reminders_at_one():
    """The day after Thanksgiving closes at 13:00 (SB-1775): an exit still waiting
    after it has lapsed, and the message says when it will."""
    assert in_session(datetime(2026, 11, 27, 12, 59)) is True
    assert in_session(datetime(2026, 11, 27, 13, 30)) is False
    assert in_session(datetime(2026, 11, 25, 13, 30)) is True  # a full session
    asked = exit_waiting_message(_waiting(datetime(2026, 11, 27, 10, 0)), 45)
    assert "Lapses unfilled at 13:00 ET" in asked


# ---------------------------------------------------------------- the watchdog


def _payload(*waiting: WaitingExit) -> dict:
    return {"mode": "live", "waiting_exits": [json.loads(w.model_dump_json()) for w in waiting]}


def test_watchdog_alarms_on_an_exit_waiting_thirty_minutes():
    stuck = _waiting(WED - timedelta(minutes=EXIT_WAIT_ALARM_MINUTES))
    [alarm] = guard_alarms(_payload(stuck), WED)
    assert "SELL XYZ" in alarm and "approval" in alarm and "30 min" in alarm
    assert exit_wait_alarms(_payload(_waiting(WED - timedelta(minutes=29))), WED) == []


def test_watchdog_is_quiet_about_waiting_exits_outside_the_session():
    evening = datetime(2026, 10, 21, 18, 0)
    assert exit_wait_alarms(_payload(_waiting(evening - timedelta(hours=4))), evening) == []


def test_watchdog_skips_a_malformed_entry_and_old_status_files():
    assert exit_wait_alarms({"waiting_exits": [{"symbol": "XYZ"}]}, WED) == []
    assert exit_wait_alarms({}, WED) == []
    assert guard_alarms({"mode": "live"}) == []  # no clock: the old call still works


# ------------------------------------------------------------- expiry is loud


def test_a_live_scan_reports_the_exits_its_sweep_expired(engine, conn, provider):  # noqa: F811
    account_id = _live_engine(engine, conn, provider)
    stale = MONDAY - timedelta(days=3)
    sell = _write(conn, account_id, stale, side=Side.SELL, status=IntentStatus.AWAITING_APPROVAL)
    _write(conn, account_id, stale, symbol="BBB", status=IntentStatus.AWAITING_APPROVAL)
    result = engine.scan(at=MONDAY)
    assert [r.intent.id for r in result.expired_exits] == [sell.id]
    stored = OrderIntentRepo(conn).get(sell.id)
    assert stored is not None and stored.status == IntentStatus.EXPIRED


def test_an_expired_exit_alerts_the_alert_chat(monkeypatch, _sent):
    _telegram(monkeypatch)
    row = OrderIntentRow(intent=_intent(), instrument=XYZ)
    result = ScanResult(run_id=1, at=WED, paper=False, scanned=1, expired_exits=[row])
    cli._notify_scan(result, "💵 live")
    texts = [m["text"] for m in _sent if m["chat_id"] == "555"]
    assert len(texts) == 1
    assert "EXPIRED unfilled" in texts[0] and "XYZ is still held" in texts[0]
    assert "approval lapsed unanswered" in texts[0]


def test_an_exit_that_never_reached_the_broker_says_so():
    row = OrderIntentRow(
        intent=_intent(approval_id=None, status=IntentStatus.PENDING), instrument=XYZ
    )
    [text] = expired_exit_messages(
        ScanResult(run_id=1, at=WED, paper=False, scanned=1, expired_exits=[row])
    )
    assert "never reached the broker" in text


# ------------------------------------------------------------------------ CLI


def _sell_waiting(cli_env: FakeProvider, monkeypatch, minutes: int) -> int:
    _cli_live(cli_env, monkeypatch)
    conn = connect(get_settings().db_path)
    account = AccountRepo(conn).get_by_name("brokerage")
    assert account is not None
    now = _Frozen.now()
    sell = _write(conn, account.id, now - timedelta(minutes=minutes), side=Side.SELL)
    OrderIntentRepo(conn).update(
        sell.id,
        status=IntentStatus.AWAITING_APPROVAL,
        approval_id="appr-9",
        submitted_at=now - timedelta(minutes=minutes),
    )
    conn.close()
    return sell.id


def test_engine_nudge_reminds_once_then_waits(cli_env: FakeProvider, monkeypatch, _sent):
    sell_id = _sell_waiting(cli_env, monkeypatch, minutes=25)
    _telegram(monkeypatch)

    first = runner.invoke(app, ["engine", "nudge", "--notify", "--json"])
    assert first.exit_code == 0, first.output
    doc = json.loads(first.output)
    assert [w["intent_id"] for w in doc["waiting"]] == [sell_id]
    assert doc["sent"] == [sell_id]
    assert [m["chat_id"] for m in _sent] == ["555"]
    assert "waiting 25 min" in _sent[0]["text"]

    again = runner.invoke(app, ["engine", "nudge", "--notify", "--json"])
    assert json.loads(again.output)["due"] == [] and len(_sent) == 1


def test_engine_nudge_without_notify_sends_and_records_nothing(
    cli_env: FakeProvider, monkeypatch, _sent
):
    _sell_waiting(cli_env, monkeypatch, minutes=25)
    _telegram(monkeypatch)
    out = runner.invoke(app, ["engine", "nudge", "--json"])
    doc = json.loads(out.output)
    assert len(doc["due"]) == 1 and doc["sent"] == [] and _sent == []
    assert not (get_settings().db_path.parent / ".exit-nudges.json").exists()


def test_an_undelivered_reminder_is_not_recorded_as_sent(cli_env: FakeProvider, monkeypatch):
    _sell_waiting(cli_env, monkeypatch, minutes=25)  # no Telegram configured
    doc = json.loads(runner.invoke(app, ["engine", "nudge", "--notify", "--json"]).stdout)
    assert len(doc["due"]) == 1 and doc["sent"] == []


def test_ack_reports_whether_the_approval_request_went_out(
    cli_env: FakeProvider, monkeypatch, _sent
):
    intent = _cli_live(cli_env, monkeypatch)
    ack = ["engine", "outbox", "ack", str(intent.id), "--approval-id", "appr-1"]

    untold = json.loads(runner.invoke(app, [*ack, "--notify", "--json"]).stdout)
    assert untold["changed"] is True and untold["notified"] is False  # no chat configured

    quiet = json.loads(runner.invoke(app, [*ack, "--json"]).output)
    assert quiet["notified"] is None  # nothing was meant to be sent


def test_ack_notified_true_when_delivered(cli_env: FakeProvider, monkeypatch, _sent):
    intent = _cli_live(cli_env, monkeypatch)
    _telegram(monkeypatch)
    out = runner.invoke(
        app,
        [
            "engine",
            "outbox",
            "ack",
            str(intent.id),
            "--approval-id",
            "appr-1",
            "--notify",
            "--json",
        ],
    )
    assert json.loads(out.output)["notified"] is True
    assert [m["chat_id"] for m in _sent] == ["555"]


def test_paper_engine_has_nothing_to_nudge(cli_env: FakeProvider):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    made = runner.invoke(app, ["engine", "init", "--symbols", "AAA"])
    assert made.exit_code == 0, made.output
    out = runner.invoke(app, ["engine", "nudge", "--json"])
    assert out.exit_code != 0 and "paper engine" in json.loads(out.stdout)["message"]
