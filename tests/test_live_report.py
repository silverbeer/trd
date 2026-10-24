"""The live engine's own post-market message (SB-1406): the account, then every
position — day change, distance to the stop, the trailing marker — and what the
session did. Refused on paper; silent on a date with no bar."""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest
from typer.testing import CliRunner

from trd.cli.app import app
from trd.config import get_settings
from trd.db.connection import connect
from trd.errors import TrdError
from trd.models import EngineMode, IntentStatus, PositionRow, Side
from trd.notify.messages import live_report_message
from trd.repos import InstrumentRepo, OrderIntentRepo, PriceRepo
from trd.repos.engine import EnginePositionRepo
from trd.services import EngineService
from trd.services.daily_report import EngineDay, ExitEvent
from trd.services.live_report import ClosedToday, LiveReport, live_report

from .conftest import FakeProvider
from .test_engine import make_bars, seed, uptrend
from .test_engine_live import _live, _names
from .test_engine_outbox import TELEGRAM_ENV

D = Decimal
runner = CliRunner()

# make_bars starts 2024-01-01 and uptrend() is 260 bars, so the newest stored
# session is 2024-09-16 and the one before it 2024-09-15.
SESSION = date(2024, 9, 16)
PREVIOUS = date(2024, 9, 15)
QTY = D("0.1")
ATR = D("5")


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


def _close(conn, symbol: str, on: date) -> Decimal:
    instrument = InstrumentRepo(conn).get_by_symbol(symbol)
    assert instrument is not None
    return next(c for d, c in PriceRepo(conn).closes_in_range(instrument.id, on, on, False))


def _open(
    engine: EngineService,
    conn,
    symbol: str,
    opened_at: datetime,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    quantity: Decimal = QTY,
    atr: Decimal = ATR,
):
    instrument = InstrumentRepo(conn).get_by_symbol(symbol)
    assert instrument is not None
    return EnginePositionRepo(conn).open(
        account_id=engine.account().id,
        instrument_id=instrument.id,
        signal_id=None,
        strategy="momentum",
        opened_at=opened_at,
        entry_price=entry,
        quantity=quantity,
        stop_price=stop,
        target_price=target,
        atr_at_entry=atr,
        last_bar_date=None,
    )


def _live_engine(engine: EngineService, conn, provider) -> None:
    _names(conn, provider, "AAA", "BBB")
    _live(engine, ["AAA", "BBB"], capital=D("100"))


# --------------------------------------------------------------------- service


def test_position_fields_day_change_distance_and_trailing(engine, conn, provider):
    _live_engine(engine, conn, provider)
    mark = _close(conn, "AAA", SESSION)
    prev = _close(conn, "AAA", PREVIOUS)
    position = _open(
        engine, conn, "AAA", datetime(2024, 9, 10, 10, 0), D("190"), D("180"), D("230")
    )
    # A high-water mark far enough up that the chandelier (high - 3 x ATR)
    # overtakes the initial 180 stop.
    EnginePositionRepo(conn).touch(position.id, D("200"), 4, SESSION)

    report = live_report(engine, "live", SESSION)

    assert report.market_open
    [p] = report.positions
    assert p.symbol == "AAA"
    assert not p.opened_today
    assert p.sessions_held == 4
    assert p.mark == mark
    assert p.day_base == prev
    assert p.day_change == (mark - prev) * D("0.1")
    assert p.unrealized == (mark - D("190")) * D("0.1")
    assert p.r_now == (mark - D("190")) / D("10")
    assert p.stop_in_force == D("185")
    assert p.trailing
    assert p.stop_distance_pct == (mark - D("185")) / mark * 100
    assert p.target_distance_pct == (D("230") - mark) / mark * 100
    # The same risk `engine positions` shows, from the same object.
    stored = EnginePositionRepo(conn).get(position.id)
    assert stored is not None
    assert (
        p.risk_at_stop
        == PositionRow(position=stored, instrument=_inst(conn), price=mark).risk_at_stop
    )
    assert report.today_change == p.day_change

    text = live_report_message(report)
    assert text.startswith("💵 <b>trd LIVE</b> · Mon Sep 16")
    assert "stop 185.00↑" in text
    assert "% away" in text


def _inst(conn):
    instrument = InstrumentRepo(conn).get_by_symbol("AAA")
    assert instrument is not None
    return instrument


def test_an_untrailed_stop_carries_no_arrow(engine, conn, provider):
    _live_engine(engine, conn, provider)
    _open(engine, conn, "AAA", datetime(2024, 9, 10, 10, 0), D("190"), D("180"), D("230"))
    [p] = live_report(engine, "live", SESSION).positions
    assert not p.trailing
    assert "stop 180.00 (" in live_report_message(live_report(engine, "live", SESSION))


def test_a_position_opened_today_measures_its_day_from_the_entry(engine, conn, provider):
    _live_engine(engine, conn, provider)
    mark = _close(conn, "BBB", SESSION)
    entry = mark - D("2")
    _open(
        engine, conn, "BBB", datetime(2024, 9, 16, 10, 0), entry, entry - D("10"), entry + D("20")
    )

    report = live_report(engine, "live", SESSION)

    [p] = report.positions
    assert p.opened_today
    assert p.day_base == entry
    assert p.day_change == D("2") * D("0.1")
    assert [o.symbol for o in report.opened_today] == ["BBB"]
    text = live_report_message(report)
    assert "new today" in text
    assert "Bought BBB" in text


def test_positions_are_listed_best_first(engine, conn, provider):
    _live_engine(engine, conn, provider)
    a = _close(conn, "AAA", SESSION)
    b = _close(conn, "BBB", SESSION)
    _open(engine, conn, "AAA", datetime(2024, 9, 10, 10, 0), a + D("5"), a - D("5"), a + D("20"))
    _open(engine, conn, "BBB", datetime(2024, 9, 10, 10, 0), b - D("5"), b - D("15"), b + D("20"))
    assert [p.symbol for p in live_report(engine, "live", SESSION).positions] == ["BBB", "AAA"]


def test_a_trade_closed_today_is_in_today(engine, conn, provider):
    _live_engine(engine, conn, provider)
    prev = _close(conn, "AAA", PREVIOUS)
    position = _open(
        engine, conn, "AAA", datetime(2024, 9, 10, 10, 0), D("190"), D("180"), D("230")
    )
    EnginePositionRepo(conn).close(
        position.id, datetime(2024, 9, 16, 11, 0), D("215"), "hit target", exit_rule="target"
    )

    report = live_report(engine, "live", SESSION)

    assert report.positions == []
    [closed] = report.closed_today
    assert closed.trade.symbol == "AAA"
    assert closed.trade.pnl == D("2.5")
    # The account moved today by the part since yesterday's close, not the
    # whole trade's P&L from entry.
    assert closed.day_change == (D("215") - prev) * D("0.1")
    assert report.today_change == closed.day_change
    text = live_report_message(report)
    assert "Sold <b>AAA</b> +$2.50" in text
    assert "Flat — all cash." in text
    assert "0 of 0" not in text


def test_an_order_that_expired_today_is_reported(engine, conn, provider):
    _live_engine(engine, conn, provider)
    instrument = _inst(conn)
    repo = OrderIntentRepo(conn)
    intent = repo.insert(
        account_id=engine.account().id,
        instrument_id=instrument.id,
        side=Side.BUY,
        quantity=D("0.05"),
        ref_price=D("200"),
        reason="test",
        strategy="momentum",
        created_at=datetime(2024, 9, 16, 10, 0),
        stop_price=D("190"),
        target_price=D("220"),
        atr_at_entry=D("5"),
    )
    repo.update(intent.id, status=IntentStatus.EXPIRED, resolved_at=datetime(2024, 9, 16, 16, 0))

    report = live_report(engine, "live", SESSION)

    assert [(u.symbol, u.status) for u in report.unfilled_today] == [("AAA", IntentStatus.EXPIRED)]
    assert "expired BUY AAA 0.05 sh" in live_report_message(report)


def test_a_paper_engine_is_refused(engine, conn, provider):
    _names(conn, provider, "AAA")
    engine.init(symbols=["AAA"], strategies=["momentum"])
    with pytest.raises(TrdError, match="paper engine"):
        live_report(engine, "live", SESSION)


def test_a_date_with_no_bar_is_no_session(engine, conn, provider):
    _live_engine(engine, conn, provider)
    report = live_report(engine, "live", SESSION + timedelta(days=1))
    assert not report.market_open
    assert "no session stored for this date" in live_report_message(report)


def test_stale_marks_are_flagged_and_leave_the_day_unknown(engine, conn, provider):
    _live_engine(engine, conn, provider)
    # BBB loses its newest bar: it is marked on the 15th while AAA has the 16th.
    instrument = InstrumentRepo(conn).get_by_symbol("BBB")
    assert instrument is not None
    conn.execute(
        "DELETE FROM price_daily WHERE instrument_id = ? AND date = ?", [instrument.id, SESSION]
    )
    _open(engine, conn, "BBB", datetime(2024, 9, 10, 10, 0), D("190"), D("180"), D("230"))

    report = live_report(engine, "live", SESSION)

    assert report.day.marks_are_stale
    assert report.day.marked_at == PREVIOUS
    [p] = report.positions
    assert p.marked_on == PREVIOUS
    assert p.day_change is None
    assert report.today_change is None
    text = live_report_message(report)
    assert text.splitlines()[2] == "⚠️ marks are stale — priced at Sep 15, not today"
    assert "— today" in text


# ------------------------------------------------------------------------- CLI


def _cli_live_home(cli_env: FakeProvider) -> None:
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
            "100",
            "--size",
            "20",
            "--symbols",
            "AAA",
        ],
    )
    assert created.exit_code == 0, created.output
    conn = connect(get_settings().db_path)
    try:
        seed(conn, "AAA", make_bars(uptrend()))
    finally:
        conn.close()


def test_cli_notify_refuses_without_a_private_chat(cli_env, monkeypatch, _no_telegram):
    _cli_live_home(cli_env)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")  # the channel: never used here
    result = runner.invoke(
        app, ["engine", "live-report", "--date", SESSION.isoformat(), "--notify", "--json"]
    )
    assert result.exit_code != 0
    assert json.loads(result.output)["error"]
    assert _no_telegram == []


def test_cli_notify_goes_to_the_alert_chat(cli_env, monkeypatch, _no_telegram):
    _cli_live_home(cli_env)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    result = runner.invoke(
        app, ["engine", "live-report", "--date", SESSION.isoformat(), "--notify", "--json"]
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["sent"] is True
    assert set(body) == {"message", "sent", "report"}
    assert [m["chat_id"] for m in _no_telegram] == ["555"]
    assert _no_telegram[0]["text"] == body["message"]


def test_cli_sends_nothing_on_a_date_with_no_bar(cli_env, monkeypatch, _no_telegram):
    _cli_live_home(cli_env)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    holiday = (SESSION + timedelta(days=1)).isoformat()
    result = runner.invoke(app, ["engine", "live-report", "--date", holiday, "--notify", "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["sent"] is False
    assert body["report"]["market_open"] is False
    assert _no_telegram == []


def test_cli_refuses_a_paper_engine(cli_env):
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="100")
    assert runner.invoke(app, ["engine", "init", "--symbols", "AAA"]).exit_code == 0
    result = runner.invoke(app, ["engine", "live-report", "--json"])
    assert result.exit_code != 0
    assert "paper engine" in json.loads(result.output)["message"]


@pytest.mark.parametrize(
    ("pct", "stop_hit", "verdict"),
    [
        ("3", False, "🚀 <b>GREAT DAY"),
        ("0.8", False, "🟢 <b>GOOD DAY"),
        ("0.1", False, "⚪ <b>FLAT DAY"),
        ("-0.1", False, "⚪ <b>FLAT DAY"),
        ("-0.8", False, "🔴 <b>DOWN DAY"),
        ("-3", False, "🩸 <b>BAD DAY"),
        # A stop taken is a bad day whatever the total says.
        ("0.8", True, "🩸 <b>BAD DAY"),
    ],
)
def test_the_day_is_called_plainly(monkeypatch, pct: str, stop_hit: bool, verdict: str) -> None:
    Model = LiveReport
    monkeypatch.setattr(Model, "today_change", property(lambda self: D(pct)))
    monkeypatch.setattr(Model, "today_change_pct", property(lambda self: D(pct)))

    trade = ExitEvent(
        symbol="AAA", strategy="pullback", rule="stop" if stop_hit else "target", pnl=D(pct)
    )
    day = EngineDay(engine="live", last_session=SESSION, capital=D("100"), mode=EngineMode.LIVE)
    report = Model(on=SESSION, day=day, closed_today=[ClosedToday(trade=trade)])
    lines = live_report_message(report).splitlines()
    assert lines[2].startswith(verdict)
    if stop_hit:
        assert "Stopped out of <b>AAA</b>" in lines[3]


def test_data_is_html_escaped() -> None:
    day = EngineDay(
        engine="live",
        last_session=SESSION,
        capital=D("100"),
        mode=EngineMode.LIVE,
        halted=True,
        halt_reason="loss <stop> & more",
    )
    text = live_report_message(LiveReport(on=SESSION, day=day))
    assert "loss &lt;stop&gt; &amp; more" in text
