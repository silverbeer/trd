"""`trd engine close`: exit ONE engine position by hand (SB-1741).

Paper books the sale at the quote. Live writes a SELL intent with the rule
'manual' — exactly what an exit rule would write — and the broker's fill closes
the position with that rule. Handed over while halted; scored out of the
per-strategy scorecard. The clock is passed in; FakeProvider supplies quotes.
"""

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import duckdb
import pytest
from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

from trd.cli import app as cli_app
from trd.cli.app import app
from trd.cli.render import outbox_renderables, replay_match_renderables
from trd.config import get_settings
from trd.db.connection import connect
from trd.engine import MANUAL_EXIT_RULE
from trd.errors import TrdError
from trd.models import EngineMode, HaltCause, IntentStatus, Outbox, PositionStatus, Side
from trd.notify.messages import expired_exit_messages, halt_message, rejected_exit_message
from trd.repos import OrderIntentRepo, TransactionRepo
from trd.repos.engine import EnginePositionRepo
from trd.services import EngineService, FillIngestService, OutboxService, replay_match
from trd.services.backtest import FillMode
from trd.services.exit_whatif import compare
from trd.services.outcomes import OutcomeService
from trd.services.review import engine_pack, review

from .conftest import FakeProvider
from .test_engine import make_bars, seed, uptrend
from .test_engine_fills import _order, _snap
from .test_engine_live import MONDAY, _filled_position, _live, _names
from .test_engine_outbox import _no_telegram
from .test_exit_whatif import DAY, FIVE, live, session
from .test_replay_match import replayed

__all__ = ["_no_telegram"]  # autouse: no inherited Telegram config

D = Decimal
runner = CliRunner()
SOON = MONDAY + timedelta(minutes=5)


@pytest.fixture
def engine(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    return EngineService(conn, provider)


def _live_holding(engine, conn, provider, *symbols: str) -> None:
    """A live engine holding 10 sh of each name at 200, stop 190, quoted at 205."""
    _names(conn, provider, *symbols)
    _live(engine, list(symbols))
    for symbol in symbols:
        _filled_position(engine, conn, symbol)
        provider.add_symbol(symbol, price="205")


def _open_intents(conn, engine):
    return [i for i, _ in OrderIntentRepo(conn).list_open(engine.account().id)]


# ----------------------------------------------------------------------- paper


def test_paper_close_sells_at_the_quote_and_closes_manual(engine, conn, provider):
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=D("10000"))
    assert len(engine.scan().opened) == 1
    provider.add_symbol("AAA", price="123.45")

    result = engine.close("aaa", reason="before the event")

    assert result.mode == EngineMode.PAPER and result.intent is None
    assert result.fill is not None and result.fill.rule == MANUAL_EXIT_RULE
    assert result.fill.price == D("123.45")
    ((position, _),) = EnginePositionRepo(conn).list_closed(engine.account().id)
    assert position.status == PositionStatus.CLOSED
    assert position.exit_rule == MANUAL_EXIT_RULE
    assert position.exit_reason == "before the event"
    sells = TransactionRepo(conn).list_chronological(engine.account().id)
    assert sells[-1].side == Side.SELL and sells[-1].quantity == result.fill.quantity
    # The owner's decision, not the strategy's: out of the scorecard.
    assert all(stat.trades == 0 for stat in engine.report())


def test_close_refuses_a_name_not_held(engine, conn, provider):
    _names(conn, provider, "AAA")
    engine.init(symbols=["AAA"], strategies=["momentum"])
    with pytest.raises(TrdError, match="no open position"):
        engine.close("AAA")
    with pytest.raises(TrdError, match="Unknown symbol"):
        engine.close("ZZZZ")


# ------------------------------------------------------------------------ live


def test_live_close_writes_a_manual_sell_and_the_fill_closes_it(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA")

    result = engine.close("AAA", at=MONDAY)

    assert result.fill is None and result.intent is not None
    intent = result.intent.intent
    assert intent.side == Side.SELL and intent.rule == MANUAL_EXIT_RULE
    assert intent.quantity == D("10") and intent.ref_price == D("205")
    assert intent.status == IntentStatus.PENDING and intent.position_id is not None
    # Nothing sold: no sell txn, the position still open.
    txns = TransactionRepo(conn).list_chronological(engine.account().id)
    assert [t.side for t in txns] == [Side.BUY]
    assert len(EnginePositionRepo(conn).list_open(engine.account().id)) == 1
    # The next scan does not ask to sell it again — even with the price through
    # the 190 stop, where a rule WOULD fire on a position with no sell in flight.
    assert [i.id for i in _open_intents(conn, engine)] == [intent.id]
    provider.add_symbol("AAA", price="185")
    rescan = engine.scan(at=SOON)
    assert [r.intent for r in rescan.intents if r.intent.side == Side.SELL] == []
    assert [i.id for i in _open_intents(conn, engine)] == [intent.id]

    FillIngestService(conn).ingest(_snap([_order(intent, "AAA", price="204.50")]), SOON)
    ((position, _),) = EnginePositionRepo(conn).list_closed(engine.account().id)
    assert position.exit_rule == MANUAL_EXIT_RULE


def test_live_close_adopts_a_pending_engine_exit_already_handed_to_the_runner(
    engine, conn, provider
):
    _live_holding(engine, conn, provider, "AAA")
    provider.add_symbol("AAA", price="185")  # through the 190 stop
    (stop,) = [r.intent for r in engine.scan(at=MONDAY).intents if r.intent.side == Side.SELL]
    assert stop.rule == "stop"
    # The runner has read the outbox and is placing this spec right now.
    (spec,) = OutboxService(conn).pending(MONDAY + timedelta(minutes=1)).orders
    assert spec.intent_id == stop.id

    result = engine.close("AAA", reason="out before the call", at=SOON)

    # Adopted in place: same intent, same ref_id, same size — never a second order.
    assert result.adopted == stop.id and result.adopted_rule == "stop"
    assert result.intent is not None
    adopted = result.intent.intent
    assert (adopted.id, adopted.ref_id, adopted.quantity) == (stop.id, stop.ref_id, stop.quantity)
    assert adopted.status == IntentStatus.PENDING
    assert adopted.rule == MANUAL_EXIT_RULE and adopted.reason == "out before the call"
    assert [i.id for i in _open_intents(conn, engine)] == [stop.id]

    # The runner's ack for the spec it already placed still lands...
    OutboxService(conn).ack(str(spec.intent_id), SOON, approval_id="appr-1")
    # ...nothing new is handed over...
    assert OutboxService(conn).pending(SOON + timedelta(minutes=1)).orders == []
    # ...and the one fill closes the position as a manual close.
    placed = OrderIntentRepo(conn).get(stop.id)
    assert placed is not None
    order = _order(placed, "AAA", price="184.00", ref_id="appr-1")
    FillIngestService(conn).ingest(_snap([order]), SOON + timedelta(minutes=5))
    ((position, _),) = EnginePositionRepo(conn).list_closed(engine.account().id)
    assert position.exit_rule == MANUAL_EXIT_RULE
    sells = [
        t
        for t in TransactionRepo(conn).list_chronological(engine.account().id)
        if t.side == Side.SELL
    ]
    assert len(sells) == 1


def test_live_close_refuses_a_pending_exit_for_part_of_the_position(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA")
    provider.add_symbol("AAA", price="185")
    (stop,) = [r.intent for r in engine.scan(at=MONDAY).intents if r.intent.side == Side.SELL]
    OrderIntentRepo(conn).update(stop.id, quantity=D("4"))  # a scale-out's piece

    with pytest.raises(TrdError, match="part of AAA"):
        engine.close("AAA", at=SOON)
    left = OrderIntentRepo(conn).get(stop.id)
    assert left is not None and left.rule == "stop" and left.status == IntentStatus.PENDING


@pytest.mark.parametrize("status", [IntentStatus.AWAITING_APPROVAL, IntentStatus.SUBMITTED])
def test_live_close_refuses_when_an_exit_is_already_with_the_broker(engine, conn, provider, status):
    _live_holding(engine, conn, provider, "AAA")
    provider.add_symbol("AAA", price="185")
    (stop,) = [r.intent for r in engine.scan(at=MONDAY).intents if r.intent.side == Side.SELL]
    OrderIntentRepo(conn).update(stop.id, status=status)

    with pytest.raises(TrdError, match="already with the broker"):
        engine.close("AAA", at=SOON)
    assert [i.id for i in _open_intents(conn, engine)] == [stop.id]


def test_live_close_refuses_a_second_manual_close(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA")
    engine.close("AAA", at=MONDAY)
    with pytest.raises(TrdError, match="already being closed by hand"):
        engine.close("AAA", at=SOON)


@pytest.mark.parametrize(
    "at",
    [
        datetime(2024, 9, 16, 16, 0),  # Monday, at the close
        datetime(2024, 9, 14, 10, 0),  # Saturday
    ],
)
def test_live_close_refuses_when_the_order_would_lapse_unplaced(engine, conn, provider, at):
    _live_holding(engine, conn, provider, "AAA")
    with pytest.raises(TrdError, match="lapse"):
        engine.close("AAA", at=at)
    assert _open_intents(conn, engine) == []


def test_live_close_before_the_open_is_allowed(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA")
    result = engine.close("AAA", at=datetime(2024, 9, 16, 8, 0))
    assert result.intent is not None


def test_a_halted_outbox_hands_over_manual_closes_and_nothing_else(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA", "BBB")
    provider.add_symbol("BBB", price="185")  # BBB's stop fires; AAA's does not
    engine.halt("pausing")
    (stop,) = [r.intent for r in engine.scan(at=MONDAY).intents if r.intent.side == Side.SELL]
    assert stop.rule == "stop"

    result = engine.close("AAA", at=MONDAY)
    assert result.halted

    outbox = OutboxService(conn).pending(SOON)
    assert outbox.halted and outbox.halt_reason == "pausing"
    assert [(o.symbol, o.rule) for o in outbox.orders] == [("AAA", MANUAL_EXIT_RULE)]
    assert outbox.withheld == []


# ------------------------------------------------------------------------- CLI


class _Wednesday(datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 10, 21, 10, 5)


def test_cli_close_live_with_rm_json(cli_env: FakeProvider, monkeypatch):
    monkeypatch.setattr("trd.services.engine.datetime", _Wednesday)
    assert runner.invoke(app, ["init"]).exit_code == 0
    cli_env.add_symbol("AAA", price="205")
    cli_env.add_symbol("BBB", price="205")
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
            "AAA,BBB",
        ],
    )
    assert created.exit_code == 0, created.output
    conn = connect(get_settings().db_path)
    _filled_position(EngineService(conn, cli_env), conn)
    _filled_position(EngineService(conn, cli_env), conn, "BBB")
    conn.close()

    human = runner.invoke(app, ["engine", "close", "BBB", "--reason", "done with it"])
    assert human.exit_code == 0, human.output
    assert "Queued" in human.output and "approve it in your broker app" in human.output

    out = runner.invoke(app, ["engine", "close", "AAA", "--rm", "--json"])
    assert out.exit_code == 0, out.output
    doc = json.loads(out.output)
    assert doc["mode"] == "live" and doc["fill"] is None
    assert doc["intent"]["intent"]["rule"] == "manual"
    assert doc["intent"]["intent"]["side"] == "sell"
    assert "closes when the manual sell fills" in doc["removed"]

    again = runner.invoke(app, ["engine", "close", "AAA", "--json"])
    assert again.exit_code != 0
    assert json.loads(again.output)["error"]


# ------------------------------------------------- ordering, sweep (SB-1741 review)

FRIDAY = datetime(2024, 9, 13, 11, 0)  # the session before MONDAY


def _sell_intent(engine, conn, symbol: str, created_at: datetime, status=IntentStatus.PENDING):
    """An exit intent on `symbol` as a scan would have written it."""
    repo = OrderIntentRepo(conn)
    account = engine.account()
    instrument = engine.instruments.get_by_symbol(symbol)
    (position,) = [p for p, i in engine.positions.list_open(account.id) if i.id == instrument.id]
    intent = repo.insert(
        account_id=account.id,
        instrument_id=instrument.id,
        side=Side.SELL,
        quantity=position.remaining_quantity,
        ref_price=D("185"),
        reason="stop hit",
        strategy=position.strategy,
        created_at=created_at,
        rule="stop",
        position_id=position.id,
    )
    if status != IntentStatus.PENDING:
        intent = repo.update(intent.id, status=status)
    return intent


def _no_network(engine, monkeypatch):
    def refuse(symbols):
        raise AssertionError(f"quote fetched before every refusal: {symbols}")

    monkeypatch.setattr(engine, "_quotes", refuse)


def test_a_refused_live_close_writes_nothing_and_never_quotes(engine, conn, provider, monkeypatch):
    _live_holding(engine, conn, provider, "AAA")
    lapsed = _sell_intent(engine, conn, "AAA", FRIDAY)  # an earlier session's, unswept
    _sell_intent(engine, conn, "AAA", MONDAY.replace(hour=9), status=IntentStatus.SUBMITTED)
    _no_network(engine, monkeypatch)

    with pytest.raises(TrdError, match="already with the broker"):
        engine.close("AAA", at=MONDAY)

    # The refusal swept nothing: the lapsed intent is the scan's to expire.
    still = OrderIntentRepo(conn).get(lapsed.id)
    assert still is not None and still.status == IntentStatus.PENDING


@pytest.mark.parametrize(
    "at",
    [datetime(2024, 9, 14, 10, 0), datetime(2024, 9, 16, 16, 0)],  # Saturday; the bell
)
def test_a_lapse_refusal_never_quotes(engine, conn, provider, monkeypatch, at):
    _live_holding(engine, conn, provider, "AAA")
    _no_network(engine, monkeypatch)
    with pytest.raises(TrdError, match="lapse"):
        engine.close("AAA", at=at)


def test_no_price_refuses_before_any_write(engine, conn, provider, monkeypatch):
    _live_holding(engine, conn, provider, "AAA")
    lapsed = _sell_intent(engine, conn, "AAA", FRIDAY)
    monkeypatch.setattr(engine, "_quotes", lambda symbols: {})
    monkeypatch.setattr(engine.prices, "latest_close", lambda instrument_id: None)

    with pytest.raises(TrdError, match="No quote and no stored close"):
        engine.close("AAA", at=MONDAY)

    still = OrderIntentRepo(conn).get(lapsed.id)
    assert still is not None and still.status == IntentStatus.PENDING
    assert [i.id for i in _open_intents(conn, engine)] == [lapsed.id]


def test_close_sweeps_only_its_own_lapsed_intents(engine, conn, provider):
    """Another name's exit lapsed overnight: the close leaves it, so the next
    scan's sweep finds it and alerts (SB-1735). Its own lapsed exit is swept."""
    _live_holding(engine, conn, provider, "AAA", "BBB")
    own = _sell_intent(engine, conn, "AAA", FRIDAY)
    other = _sell_intent(engine, conn, "BBB", FRIDAY)

    closed = engine.close("AAA", at=datetime(2024, 9, 16, 8, 0))  # pre-market

    assert closed.expired == [own.id] and closed.adopted is None
    swept = OrderIntentRepo(conn).get(own.id)
    assert swept is not None and swept.status == IntentStatus.EXPIRED
    untouched = OrderIntentRepo(conn).get(other.id)
    assert untouched is not None and untouched.status == IntentStatus.PENDING

    scan = engine.scan(at=datetime(2024, 9, 16, 9, 35))
    assert [r.intent.id for r in scan.expired_exits] == [other.id]
    assert len(expired_exit_messages(scan, "live")) == 1


# ------------------------------------------------------- alerts (SB-1741 review)


def test_a_lapsed_manual_close_alert_asks_for_it_again(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA")
    intent = engine.close("AAA", at=MONDAY).intent.intent  # type: ignore[union-attr]

    scan = engine.scan(at=datetime(2024, 9, 16, 16, 0))  # the day's last scan

    assert [r.intent.id for r in scan.expired_exits] == [intent.id]
    (msg,) = expired_exit_messages(scan, "live")
    assert "manual close of AAA expired unapproved" in msg
    assert "Run 'trd engine close AAA' again in session" in msg
    # Neither promise of the rule-exit alert holds for a manual close.
    assert "re-issues" not in msg and "sell in your broker app" not in msg


def test_a_rejected_rule_exit_says_the_scan_decides_again(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA")
    provider.add_symbol("AAA", price="185")
    (stop,) = [r.intent for r in engine.scan(at=MONDAY).intents if r.intent.side == Side.SELL]
    ack = OutboxService(conn).ack(str(stop.id), SOON, error="503 <busy>")
    msg = rejected_exit_message(ack, "live")
    assert "REJECTED by the broker: 503 &lt;busy&gt;" in msg
    assert "next scan in session decides the exit again" in msg
    assert "trd engine close" not in msg


def _cli_live_home(cli_env: FakeProvider, monkeypatch, *symbols: str) -> None:
    monkeypatch.setattr("trd.services.engine.datetime", _Wednesday)
    assert runner.invoke(app, ["init"]).exit_code == 0
    for symbol in symbols:
        cli_env.add_symbol(symbol, price="205")
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
            ",".join(symbols),
        ],
    )
    assert created.exit_code == 0, created.output
    conn = connect(get_settings().db_path)
    for symbol in symbols:
        _filled_position(EngineService(conn, cli_env), conn, symbol)
    conn.close()


def test_cli_rejected_manual_close_alerts_the_private_chat(
    cli_env: FakeProvider, monkeypatch, _no_telegram
):
    _cli_live_home(cli_env, monkeypatch, "AAA")
    closed = runner.invoke(app, ["engine", "close", "AAA", "--json"])
    assert closed.exit_code == 0, closed.output
    intent_id = json.loads(closed.output)["intent"]["intent"]["id"]

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("TRD_LIVE_ALERT_CHAT_ID", "555")
    out = runner.invoke(
        app,
        [
            "engine",
            "outbox",
            "ack",
            str(intent_id),
            "--error",
            "place failed",
            "--notify",
            "--json",
        ],
    )

    assert out.exit_code == 0, out.output
    doc = json.loads(out.output)
    assert doc["to_status"] == "rejected" and doc["notified"] is True
    (sent,) = _no_telegram
    assert sent["chat_id"] == "555"  # the private alert chat, never the channel
    assert "manual close of AAA was REJECTED by the broker: place failed" in sent["text"]
    assert "run 'trd engine close AAA' again" in sent["text"]


# --------------------------------------------- free-text reasons (SB-1741 review)


def _paper_holding_cli(cli_env: FakeProvider) -> None:
    assert runner.invoke(app, ["init"]).exit_code == 0
    conn = connect(get_settings().db_path)
    engine = EngineService(conn, cli_env)
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    cli_env.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=D("10000"))
    assert len(engine.scan().opened) == 1
    cli_env.add_symbol("AAA", price="123.45")
    conn.close()


def _wide(monkeypatch) -> None:
    """Tables wide enough that a cell is never wrapped across lines."""
    monkeypatch.setattr(cli_app.console, "width", 250)


@pytest.mark.parametrize("reason", ["trim [/] now", "[earnings] out"])
def test_cli_paper_close_prints_a_bracketed_reason_as_typed(
    cli_env: FakeProvider, monkeypatch, reason
):
    _wide(monkeypatch)
    _paper_holding_cli(cli_env)

    closed = runner.invoke(app, ["engine", "close", "AAA", "--reason", reason])
    assert closed.exit_code == 0, closed.output
    assert "Closed" in closed.output and reason in closed.output

    listed = runner.invoke(app, ["engine", "positions", "--all"])
    assert listed.exit_code == 0, listed.output
    assert reason in listed.output


def test_cli_live_intents_and_outbox_print_a_bracketed_reason(cli_env: FakeProvider, monkeypatch):
    _wide(monkeypatch)
    _cli_live_home(cli_env, monkeypatch, "AAA")
    reason = "trim [/] now"
    closed = runner.invoke(app, ["engine", "close", "AAA", "--reason", reason])
    assert closed.exit_code == 0, closed.output

    intents = runner.invoke(app, ["engine", "intents"])
    assert intents.exit_code == 0, intents.output
    assert reason in intents.output

    monkeypatch.setattr("trd.cli.app.datetime", _Wednesday)
    outbox = runner.invoke(app, ["engine", "outbox"])
    assert outbox.exit_code == 0, outbox.output
    assert reason in outbox.output


# ------------------------------------------------------ halt (SB-1741 review)


def test_halted_texts_say_manual_closes_still_go_out():
    assert "except manual closes" in halt_message("loss stop")
    rendered = outbox_renderables(
        Outbox(account="brokerage", at=MONDAY, halted=True, halt_reason="[runner] odd call")
    )
    text = " ".join(r.plain for r in rendered if isinstance(r, Text))
    assert "only manual closes are handed over" in text and "[runner] odd call" in text


def test_halting_a_halted_engine_keeps_the_loss_stop_cause(engine, conn, provider):
    _live_holding(engine, conn, provider, "AAA")
    config = engine.config()
    first = engine.guards.halt(config, "loss stop: -20", MONDAY, cause=HaltCause.MAX_LOSS)

    again = engine.halt("live runner: order call no spec asked for", at=SOON)

    assert again.halted and again.halted_by == HaltCause.MAX_LOSS
    assert again.halted_at == first.halted_at == MONDAY
    assert again.halt_reason is not None
    assert "loss stop: -20" in again.halt_reason and "no spec asked for" in again.halt_reason
    # The same reason twice is not appended twice.
    assert engine.halt("live runner: order call no spec asked for", at=SOON) == again


# ----------------------------------------------- scorecards (SB-1741 review)


def test_cli_report_with_only_manual_closes_says_so(cli_env: FakeProvider):
    _paper_holding_cli(cli_env)
    assert runner.invoke(app, ["engine", "close", "AAA"]).exit_code == 0

    report = runner.invoke(app, ["engine", "report"])

    assert report.exit_code == 0, report.output
    assert "No trades yet" not in report.output
    assert "1 trade(s) closed by hand" in report.output
    assert json.loads(runner.invoke(app, ["engine", "report", "--json"]).output) == []


def _manual_closes(conn, provider, n: int) -> EngineService:
    """n paper positions, every one closed by hand on a strictly rising tape."""
    symbols = [chr(ord("A") + i) * 3 for i in range(n)]
    bars = make_bars([100.0 + i for i in range(260)])
    for symbol in symbols:
        seed(conn, symbol, bars)
        provider.add_symbol(symbol, price="282")
    engine = EngineService(conn, provider)
    engine.init(symbols=symbols, strategies=["momentum"], position_size=D("10000"), max_positions=n)
    account = engine.account()
    opened = datetime(2024, 6, 29, 10, 0)
    for symbol in symbols:
        instrument = engine.instruments.get_by_symbol(symbol)
        assert instrument is not None
        TransactionRepo(conn).insert(
            account_id=account.id,
            instrument_id=instrument.id,
            side=Side.BUY,
            quantity=D("10"),
            price=D("280"),
            fees=D(0),
            executed_at=opened,
            note="fill",
        )
        EnginePositionRepo(conn).open(
            account_id=account.id,
            instrument_id=instrument.id,
            signal_id=None,
            strategy="momentum",
            opened_at=opened,
            entry_price=D("280"),
            quantity=D("10"),
            stop_price=D("278"),
            target_price=D("300"),
            atr_at_entry=D("1"),
            last_bar_date=opened.date(),
        )
    for symbol in symbols:
        engine.close(symbol, reason="before the event", at=datetime(2024, 7, 9, 15, 0))
    OutcomeService(conn).backfill()
    return engine


def test_the_nightly_review_leaves_manual_closes_out_of_its_window(conn, provider):
    engine = _manual_closes(conn, provider, 5)
    on = date(2024, 7, 15)

    pack = engine_pack(engine, OutcomeService(conn), "swing", on)

    # Measured, but not a population any finding about a rule is drawn from.
    assert OutcomeService(conn).trades.list_all()
    assert pack.window_trades == []
    assert [f.key for f in review([pack], on).findings] == []


def test_replay_match_drops_a_manual_close_with_its_replay_twin(monkeypatch):
    bars = session({(10, 10): "89"})  # the rules stop both at 10:10
    closed = [
        (live(exit_price="95", rule=MANUAL_EXIT_RULE, pid=1), SimpleNamespace(symbol="AAA")),
        (live(exit_price="89", rule="stop", pid=2), SimpleNamespace(symbol="BBB")),
    ]
    trades = [replayed(symbol="AAA", r="-1.1"), replayed(symbol="BBB", r="-1.1")]

    class _Source(replay_match.BarSource):
        def stored(self, instrument_id, limit=None):
            return bars

    class _Backtest:
        def __init__(self, conn):
            pass

        def run(self, **kw):
            return SimpleNamespace(trades=trades, end=date(2026, 8, 5), skipped=[])

    monkeypatch.setattr(replay_match, "BarSource", _Source)
    monkeypatch.setattr(replay_match, "BacktestService", _Backtest)
    service = replay_match.ReplayMatchService.__new__(replay_match.ReplayMatchService)
    service.conn = None  # type: ignore[assignment]
    service.prices = None  # type: ignore[assignment]
    service.configs = SimpleNamespace(  # type: ignore[assignment]
        get=lambda: SimpleNamespace(timeframe="5m", exit_params=DAY, account_id=1)
    )
    service.positions = SimpleNamespace(list_closed=lambda account_id: closed)  # type: ignore[assignment]

    result = service.run(start=date(2026, 8, 1))

    assert (result.live_trades, result.replay_trades, result.matched) == (1, 1, 1)
    assert result.replay_only_by_strategy == {}
    assert result.excluded == {
        "closed by hand (manual)": 1,
        "replay twin of a manual close": 1,
    }
    console = Console(width=160, record=True)
    for renderable in replay_match_renderables(result):
        console.print(renderable)
    assert "1 closed by hand (manual)" in console.export_text()


def test_exit_whatif_scores_a_manual_close_only_on_its_entry():
    stopped = (live(pid=1), session({(10, 10): "89"}))
    drift = session({(10, 30): "106"}, default="102")

    def run(exit_price: str):
        manual = (live(exit_price=exit_price, rule=MANUAL_EXIT_RULE, pid=2), drift)
        return compare([stopped, manual], FIVE, DAY, [{"target_r": 0.5}], FillMode.CLOSE)

    actual, baseline, (arm,), excluded, gap, same = run("103")
    # The entry stays in the paired replay; the person's exit is out of what the
    # live trades booked.
    assert baseline.trades == 2 and arm.trades == 2
    assert actual.trades == 1 and actual.rules == {"stop": 1}
    assert same == 100.0
    assert excluded == {"closed by hand (manual): out of 'actual' and the replay error": 1}
    # Where the person sold moves none of it.
    assert run("97")[:3] == (actual, baseline, [arm]) and run("97")[4:] == (gap, same)


def test_cli_close_releases_the_database_before_telegram(
    cli_env: FakeProvider, monkeypatch, _no_telegram
):
    _cli_live_home(cli_env, monkeypatch, "AAA")
    opened: list[EngineService] = []

    def service() -> EngineService:
        opened.append(EngineService(connect(get_settings().db_path), cli_env))
        return opened[-1]

    def send(messages, alert=False):
        # The writer lock a scan waits on must already be free.
        with pytest.raises(duckdb.ConnectionException):
            opened[-1].conn.execute("SELECT 1")
        return True

    monkeypatch.setattr("trd.cli.app._engine_service", service)
    monkeypatch.setattr("trd.cli.app._send", send)
    out = runner.invoke(app, ["engine", "close", "AAA", "--notify"])
    assert out.exit_code == 0, out.output
    assert len(opened) == 1
