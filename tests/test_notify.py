import json
from datetime import datetime
from decimal import Decimal
from email.message import Message

import pytest

from trd.errors import NotifyError
from trd.notify import close_message, open_message, scan_messages
from trd.notify.telegram import TelegramNotifier, from_env
from trd.services.engine import ScanFill, ScanResult, ScanSignal, scan_events


def _result(**overrides) -> ScanResult:
    base = ScanResult(
        run_id=7,
        at=datetime(2026, 7, 28, 10, 30),
        paper=True,
        scanned=10,
        open_positions=1,
        capacity=4,
    )
    return base.model_copy(update=overrides)


OPEN_FILL = ScanFill(
    symbol="GOOGL",
    strategy="pullback",
    quantity=Decimal("3"),
    price=Decimal("326.56"),
    reason="RSI bottomed at 31 and has turned up to 37",
)
CLOSE_FILL = ScanFill(
    symbol="GOOGL",
    strategy="pullback",
    quantity=Decimal("3"),
    price=Decimal("300.00"),
    reason="hit the stop at 303.90 — thesis broke",
    rule="stop",
    pnl=Decimal("-79.68"),
    r_multiple=Decimal("-1.05"),
)


# ------------------------------------------------------------------- ndjson


def test_scan_events_are_one_flat_dict_each():
    events = scan_events(_result(opened=[OPEN_FILL], closed=[CLOSE_FILL]))
    kinds = [e["ev"] for e in events]
    assert kinds == ["close", "open", "scan"]  # closes first, summary last
    for event in events:
        # Every event must survive a JSON round trip — it is a log line.
        assert json.loads(json.dumps(event)) == event


def test_scan_event_summary_counts_everything():
    result = _result(opened=[OPEN_FILL], closed=[CLOSE_FILL], skipped=["AAA: no history"])
    summary = scan_events(result)[-1]
    assert summary["ev"] == "scan"
    assert summary["run_id"] == 7
    assert summary["opened"] == 1
    assert summary["closed"] == 1
    assert summary["skipped"] == 1
    assert summary["open_positions"] == 1


def test_scan_event_numbers_are_numeric_not_strings():
    """Grafana has to be able to graph these without a parse step."""
    event = scan_events(_result(closed=[CLOSE_FILL]))[0]
    assert isinstance(event["price"], float)
    assert isinstance(event["pnl"], float)
    assert isinstance(event["r_multiple"], float)
    assert event["pnl"] == pytest.approx(-79.68)


def test_a_quiet_scan_still_emits_its_summary():
    events = scan_events(_result())
    assert [e["ev"] for e in events] == ["scan"]


# ----------------------------------------------------------------- messages


def test_open_message_names_the_trade_and_the_why():
    text = open_message(OPEN_FILL)
    assert "BUY GOOGL" in text
    assert "x3" in text
    assert "326.56" in text
    assert "pullback" in text
    assert "RSI bottomed" in text


def test_close_message_leads_with_the_result():
    text = close_message(CLOSE_FILL)
    assert "SELL GOOGL" in text
    assert "-79.68" in text
    assert "-1.05R" in text
    assert "stop" in text


def test_only_fills_are_pushed():
    """Signals the engine declined stay in the log — pushing them would train you
    to ignore the channel."""
    seen_but_declined = ScanSignal(
        symbol="AAPL", strategy="momentum", score=0.5, reason="x", price=Decimal("100")
    )
    assert scan_messages(_result(signals=[seen_but_declined])) == []


def test_closes_are_reported_before_opens():
    messages = scan_messages(_result(opened=[OPEN_FILL], closed=[CLOSE_FILL]))
    assert len(messages) == 2
    assert messages[0].startswith("🔴")  # the loss closed
    assert messages[1].startswith("🟢")


# ----------------------------------------------------------------- telegram


def test_from_env_returns_none_when_unconfigured():
    assert from_env({}) is None
    assert from_env({"TELEGRAM_BOT_TOKEN": "abc"}) is None  # chat id missing
    assert from_env({"TELEGRAM_CHAT_ID": "123"}) is None  # token missing
    assert from_env({"TELEGRAM_BOT_TOKEN": "  ", "TELEGRAM_CHAT_ID": "123"}) is None


def test_from_env_builds_a_notifier_when_both_are_set():
    notifier = from_env({"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "-100"})
    assert isinstance(notifier, TelegramNotifier)
    assert notifier.chat_id == "-100"


def test_send_posts_json_to_the_bot_api(monkeypatch):
    """No network: the urlopen call is captured and inspected."""
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("trd.notify.telegram.urllib.request.urlopen", fake_urlopen)
    TelegramNotifier("tok", "-100").send("hello")

    assert captured["url"] == "https://api.telegram.org/bottok/sendMessage"
    assert captured["body"]["chat_id"] == "-100"
    assert captured["body"]["text"] == "hello"
    assert "parse_mode" not in captured["body"]  # plain text — reasons contain % and —
    assert captured["timeout"] == 10


def test_network_failure_raises_notify_error(monkeypatch):
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("trd.notify.telegram.urllib.request.urlopen", boom)
    with pytest.raises(NotifyError, match="Could not reach Telegram"):
        TelegramNotifier("tok", "-100").send("hello")


def test_http_error_never_leaks_the_token(monkeypatch):
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", Message(), None)

    monkeypatch.setattr("trd.notify.telegram.urllib.request.urlopen", boom)
    with pytest.raises(NotifyError) as exc:
        TelegramNotifier("supersecrettoken", "-100").send("hello")
    assert "supersecrettoken" not in str(exc.value)
    assert "401" in str(exc.value)
