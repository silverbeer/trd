import json
from datetime import datetime
from decimal import Decimal
from email.message import Message

import pytest

from trd.errors import NotifyError
from trd.notify import close_message, open_message, scan_messages
from trd.notify.telegram import TelegramNotifier, from_env, label_from_env
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
    entry_price=Decimal("326.56"),
    opened_at=datetime(2026, 7, 28, 9, 45),
    closed_at=datetime(2026, 7, 28, 11, 59),
    stop_price=Decimal("303.90"),
    target_price=Decimal("371.88"),
    risk_per_share=Decimal("22.66"),
    planned_1r=Decimal("67.98"),
    setup="RSI bottomed at 31 and has turned up to 37",
    trigger_price=Decimal("303.90"),
)
# An exit with no level to slip against — the case the Execution section drops.
INDICATOR_FILL = CLOSE_FILL.model_copy(
    update={
        "rule": "indicator",
        "reason": "closed below the 20-session average",
        "trigger_price": None,
    }
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


def test_close_message_leads_with_the_outcome():
    """ "SELL" is true of every exit and says nothing. The outcome is what a reader
    is scanning for."""
    text = close_message(CLOSE_FILL)
    assert "GOOGL — STOPPED OUT" in text
    assert "-79.68" in text
    assert "-1.05R" in text


def test_close_message_carries_the_whole_trade():
    text = close_message(CLOSE_FILL)
    assert "Entry: 326.56" in text
    assert "Exit: 300.00" in text
    assert "Held: 2h 14m" in text
    assert "Stop: 303.90" in text
    assert "Risk: 22.66/share" in text
    assert "Target: 371.88" in text
    assert "Setup: RSI bottomed" in text


def test_planned_1r_and_realized_r_are_not_conflated():
    """One is what was put at risk, the other what came back in those units.
    Reading them as the same number is how a losing rule looks fine."""
    text = close_message(CLOSE_FILL)
    assert "1R: 67.98" in text  # planned, in dollars
    assert "(-1.05R)" in text  # realized, in R
    assert "1R: -1.05" not in text


def test_a_stop_out_separates_the_trigger_from_the_fill():
    """The stop said 303.90 and the trade left at 300.00. A message that shows one
    number cannot tell you the rule worked and the execution did not."""
    text = close_message(CLOSE_FILL)
    assert "Triggered: 303.90" in text
    assert "Filled: 300.00" in text
    assert "Slippage: -3.90/share (-11.70)" in text


def test_an_exit_with_no_level_has_no_execution_section():
    """An indicator exit names no price, so there is nothing to have slipped
    against. Printing dashes there reads as broken rather than inapplicable."""
    text = close_message(INDICATOR_FILL)
    assert "Execution" not in text
    assert "Slippage" not in text
    assert "THESIS BROKEN" in text


def test_an_entry_states_what_it_risks_before_it_risks_it():
    fill = OPEN_FILL.model_copy(
        update={
            "stop_price": Decimal("303.90"),
            "target_price": Decimal("371.88"),
            "risk_per_share": Decimal("22.66"),
            "planned_1r": Decimal("67.98"),
        }
    )
    text = open_message(fill)
    assert "BUY GOOGL" in text
    assert "Stop: 303.90" in text
    assert "1R: 67.98" in text


def test_a_fill_with_no_lifecycle_still_renders():
    """Older stored results, and any fill built without a position behind it."""
    bare = ScanFill(
        symbol="AAA",
        strategy="momentum",
        quantity=Decimal("1"),
        price=Decimal("10"),
        reason="x",
        rule="time",
    )
    text = close_message(bare)
    assert "AAA — TIME EXIT" in text
    assert "—" not in text.replace("AAA — TIME EXIT", "")  # no dash-padded rows


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


# -------------------------------------------------------------- engine label


def test_messages_name_the_engine_that_sent_them():
    """Two engines share one chat and MSFT sits in both universes — without the
    label a fill can't say whether it will be held overnight."""
    messages = scan_messages(_result(opened=[OPEN_FILL], closed=[CLOSE_FILL]), label="trd-day")
    assert all(m.startswith("[trd-day] ") for m in messages)
    assert "BUY GOOGL" in messages[1]  # the label prefixes, it doesn't replace


def test_label_falls_back_to_the_rule_set():
    """A flat_at_minute is what makes an engine a day engine; everything else
    carries overnight. So the label is right with no configuration at all."""
    assert label_from_env({"flat_at_minute": 1555.0}, env={}) == "day"
    assert label_from_env({"flat_at_minute": 0.0}, env={}) == "swing"
    assert label_from_env(None, env={}) == "swing"


def test_explicit_label_wins_over_the_fallback():
    env = {"TRD_ENGINE_LABEL": "trd-day"}
    assert label_from_env({"flat_at_minute": 0.0}, env=env) == "trd-day"
    assert label_from_env({}, env={"TRD_ENGINE_LABEL": "  "}) == "swing"  # blank is unset


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
