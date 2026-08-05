"""The Telegram command bot: parsing, authorization, the queue, and the drain.

No network: the bot talks to a Transport, and the tests hand it a fake one. That
is the same seam MarketDataProvider uses, and for the same reason.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from trd.errors import NotifyError
from trd.models import DailyBar
from trd.notify.bot import (
    BotConfigError,
    CommandBot,
    EngineTarget,
    ParseError,
    allowed_users_from_env,
    engines_from_env,
    format_status,
    parse,
)
from trd.repos import EnginePositionRepo, InstrumentRepo
from trd.services import EngineService
from trd.services.commands import (
    CommandQueueService,
    QueuedCommand,
    enqueue,
    pending,
)

from .conftest import FakeProvider


class FakeTransport:
    """Records what was sent, replays what was queued. Never touches a socket."""

    def __init__(self, batches: list[list[dict[str, Any]]] | None = None) -> None:
        self.batches = batches or []
        self.sent: list[tuple[str, str]] = []
        self.offsets: list[int] = []
        self.menu: list[tuple[str, str]] = []
        self.fail_send = False

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        self.offsets.append(offset)
        return self.batches.pop(0) if self.batches else []

    def send(self, chat_id: str, text: str) -> None:
        if self.fail_send:
            raise NotifyError("nope")
        self.sent.append((chat_id, text))

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.menu = list(commands)

    # convenience for assertions
    @property
    def replies(self) -> list[str]:
        return [text for _, text in self.sent]


ME = 4242
OTHER = 9999


def message(text: str, update_id: int = 1, user_id: int = ME, chat_type: str = "private"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id, "is_bot": False, "username": "silverbeer"},
            "chat": {"id": user_id, "type": chat_type},
            "text": text,
        },
    }


@pytest.fixture
def homes(tmp_path: Path) -> list[EngineTarget]:
    targets = [
        EngineTarget("swing", tmp_path / "swing"),
        EngineTarget("day", tmp_path / "day"),
    ]
    for target in targets:
        target.home.mkdir()
    return targets


@pytest.fixture
def bot(homes: list[EngineTarget], tmp_path: Path) -> CommandBot:
    state = tmp_path / "state"
    state.mkdir()
    return CommandBot(
        transport=FakeTransport(),
        engines=homes,
        allowed_user_ids={ME},
        state_dir=state,
    )


# ------------------------------------------------------------------ parsing


def test_add_with_no_engines_named_targets_every_engine(homes):
    command = parse("/add PLTR", homes)
    assert command.kind == "add"
    assert command.symbol == "PLTR"
    assert [e.name for e in command.engines] == ["swing", "day"]


def test_add_can_name_both_engines_explicitly(homes):
    command = parse("/add pltr day swing", homes)
    assert command.symbol == "PLTR"
    assert [e.name for e in command.engines] == ["day", "swing"]


def test_naming_the_same_engine_twice_queues_it_once(homes):
    command = parse("/add PLTR day day", homes)
    assert [e.name for e in command.engines] == ["day"]


def test_unknown_engine_is_rejected_at_parse_time(homes):
    with pytest.raises(ParseError, match="Unknown engine"):
        parse("/add PLTR crypto", homes)


def test_group_style_command_suffix_is_stripped(homes):
    assert parse("/status@trd_engine_bot", homes).kind == "status"


def test_read_commands_work_without_a_slash(homes):
    assert parse("status", homes).kind == "status"
    assert parse("report day", homes).kind == "report"


def test_positions_is_an_alias_for_book(homes):
    assert parse("/positions", homes).kind == "book"


def test_add_without_a_symbol_says_what_to_type(homes):
    with pytest.raises(ParseError, match="needs a symbol"):
        parse("/add", homes)


@pytest.mark.parametrize("bad", ["/add 123", "/add ../../etc/passwd", "/add DROP TABLE"])
def test_symbols_that_are_not_symbols_are_refused(bad, homes):
    with pytest.raises(ParseError):
        parse(bad, homes)


def test_dotted_and_dashed_symbols_are_accepted(homes):
    assert parse("/add BRK.B", homes).symbol == "BRK.B"
    assert parse("/add BTC-USD", homes).symbol == "BTC-USD"


def test_unknown_verb_points_at_help(homes):
    with pytest.raises(ParseError, match="Try /help"):
        parse("/liquidate everything", homes)


# ------------------------------------------------------------ authorization


def test_a_stranger_gets_no_reply_and_nothing_queued(bot, homes):
    bot.handle(message("/add PLTR", user_id=OTHER))
    assert bot.transport.replies == []  # silence, not a refusal
    assert pending(homes[0].home) == []


def test_a_channel_post_is_ignored_even_from_an_allowed_user(bot, homes):
    # The fills channel is one-way. A channel post carries no reliable sender,
    # so there would be nobody to authorize.
    bot.handle(message("/add PLTR", chat_type="channel"))
    assert bot.transport.replies == []
    assert pending(homes[0].home) == []


def test_a_message_with_no_sender_id_is_ignored(bot):
    update = message("/status")
    del update["message"]["from"]["id"]
    bot.handle(update)
    assert bot.transport.replies == []


def test_a_matching_username_does_not_grant_access(bot):
    # Usernames are changeable and re-registerable; only the numeric id counts.
    update = message("/status", user_id=OTHER)
    update["message"]["from"]["username"] = "silverbeer"
    bot.handle(update)
    assert bot.transport.replies == []


def test_the_owner_is_answered(bot):
    bot.handle(message("/help"))
    assert "trd engine bot" in bot.transport.replies[0]


def test_a_bot_with_no_allowlist_refuses_to_start(homes, tmp_path):
    with pytest.raises(BotConfigError, match="unrestricted"):
        CommandBot(FakeTransport(), homes, allowed_user_ids=set(), state_dir=tmp_path)


def test_a_bot_with_no_engines_refuses_to_start(tmp_path):
    with pytest.raises(BotConfigError, match="No engines"):
        CommandBot(FakeTransport(), [], allowed_user_ids={ME}, state_dir=tmp_path)


# -------------------------------------------------------------------- queue


def test_add_queues_one_file_per_named_engine(bot, homes):
    bot.handle(message("/add PLTR day swing", update_id=7))
    for target in homes:
        queued = pending(target.home)
        assert [(c.symbol, c.kind, c.user_id) for c in queued] == [("PLTR", "add", ME)]
    assert "queued" in bot.transport.replies[0]
    assert "day" in bot.transport.replies[0] and "swing" in bot.transport.replies[0]


def test_naming_one_engine_leaves_the_other_alone(bot, homes):
    bot.handle(message("/add PLTR day"))
    assert pending(homes[0].home) == []  # swing
    assert len(pending(homes[1].home)) == 1  # day


def test_queue_replays_in_the_order_the_commands_were_typed(tmp_path):
    home = tmp_path / "engine"
    home.mkdir()
    # Deliberately out of order, and across the digit-width boundary that a
    # naive lexical sort gets wrong.
    for update_id, symbol in [(11, "CCC"), (2, "AAA"), (100, "DDD"), (9, "BBB")]:
        enqueue(home, update_id=update_id, kind="add", symbol=symbol, user_id=ME)
    assert [c.symbol for c in pending(home)] == ["AAA", "BBB", "CCC", "DDD"]


def test_an_unreadable_queue_file_is_retired_not_retried(tmp_path):
    home = tmp_path / "engine"
    (home / "commands").mkdir(parents=True)
    (home / "commands" / "00000000000000000001.json").write_text("{not json")
    assert pending(home) == []
    # Retired, so it cannot block everything queued behind it on every scan.
    assert not (home / "commands" / "00000000000000000001.json").exists()
    assert (home / "commands" / "done" / "00000000000000000001.json").exists()


def test_a_partly_written_queue_file_is_never_read(tmp_path):
    home = tmp_path / "engine"
    home.mkdir()
    enqueue(home, update_id=1, kind="add", symbol="AAA", user_id=ME)
    # The write goes through .tmp and renames, so a scan globbing *.json during
    # a write cannot see a half-written document.
    assert list((home / "commands").glob("*.tmp")) == []


# ------------------------------------------------------------------ offset


def test_the_offset_advances_past_handled_updates(bot):
    bot.transport.batches = [[message("/help", update_id=42)]]
    bot.poll_once(timeout=0)
    assert bot.offset() == 43


def test_the_offset_survives_a_restart(homes, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    first = CommandBot(FakeTransport([[message("/help", update_id=7)]]), homes, {ME}, state)
    first.poll_once(timeout=0)

    transport = FakeTransport()
    second = CommandBot(transport, homes, {ME}, state)
    assert second.offset() == 8
    second.poll_once(timeout=0)
    assert transport.offsets == [8]  # asks Telegram only for what is new


def test_a_command_that_blows_up_is_still_acked(bot, monkeypatch):
    """A poison pill must not be redelivered forever — that takes the whole
    channel down, not just the one message."""

    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(bot, "run", boom)
    bot.transport.batches = [[message("/status", update_id=5)]]
    with pytest.raises(RuntimeError):
        bot.poll_once(timeout=0)
    assert bot.offset() == 6


def test_a_failed_reply_does_not_lose_the_queued_command(bot, homes):
    bot.transport.fail_send = True
    bot.handle(message("/add PLTR"))
    assert bot.transport.replies == []
    # The queue is the durable record; the chat is not.
    assert len(pending(homes[0].home)) == 1


# ------------------------------------------------------------------- reads


STATUS_JSON = """
{"build": "0.1.9", "account": "rh-agent", "timeframe": "1d", "universe": ["AAA", "BBB"],
 "open_positions": 2, "max_positions": 5, "closed_trades": 11,
 "realized": "120.50", "unrealized": "-30.25", "net_pnl": "90.25",
 "committed": "2000.00", "risk_at_stop": "180.00",
 "bars_total": 5200, "bars_last": "2026-08-04", "last_scan": "2026-08-04T15:55:00",
 "warmup_bars": 200, "bar_unit": "day", "short_history": [["CCC", 12]]}
"""


def test_status_reads_the_published_snapshot_not_the_database(bot, homes):
    (homes[0].home / "status.json").write_text(STATUS_JSON)
    bot.handle(message("/status swing"))
    reply = bot.transport.replies[0]
    assert "rh-agent" in reply
    assert "realized +120.50" in reply
    assert "NET +90.25" in reply  # all three, never the total alone
    assert "at risk +180.00" in reply
    assert "CCC(12)" in reply  # warns about names that cannot trade yet


def test_status_before_the_first_scan_says_so(bot):
    bot.handle(message("/status"))
    assert "no snapshot yet" in bot.transport.replies[0]


def test_a_corrupt_snapshot_does_not_crash_the_bot(bot, homes):
    (homes[0].home / "status.json").write_text("{truncated")
    bot.handle(message("/status swing"))
    assert "unreadable" in bot.transport.replies[0]


def test_status_with_no_engine_named_covers_both(bot, homes):
    for target in homes:
        (target.home / "status.json").write_text(STATUS_JSON)
    bot.handle(message("/status"))
    reply = bot.transport.replies[0]
    assert "[swing]" in reply and "[day]" in reply


def test_a_refused_config_is_surfaced_in_status():
    text = format_status({"config_refused": "A day engine on 1d bars.", "universe": []})
    assert "⚠" in text and "refuse" in text


def test_report_leads_with_expectancy(bot, homes):
    (homes[0].home / "report.json").write_text(
        '[{"strategy": "pullback", "trades": 9, "win_rate": 55.5, "expectancy_r": 0.42}]'
    )
    bot.handle(message("/report swing"))
    reply = bot.transport.replies[0]
    assert reply.index("expectancy") < reply.index("% win")
    assert "+0.42R" in reply


def test_report_with_no_closed_trades_says_so(bot, homes):
    (homes[0].home / "report.json").write_text("[]")
    bot.handle(message("/report swing"))
    assert "no closed trades" in bot.transport.replies[0]


def test_book_sends_the_published_text(bot, homes):
    (homes[0].home / "status.txt").write_text("trd engine — last scan ...\nAAA 10 @ 100")
    bot.handle(message("/book swing"))
    assert "AAA 10 @ 100" in bot.transport.replies[0]


def test_a_long_reply_is_chunked_rather_than_dropped(bot, homes):
    (homes[0].home / "status.txt").write_text("x" * 9000)
    bot.handle(message("/book swing"))
    assert len(bot.transport.sent) == 3
    assert all(len(text) <= 4096 for _, text in bot.transport.sent)


# --------------------------------------------------------------------- env


def test_engines_parse_from_one_env_string():
    targets = engines_from_env({"TRD_BOT_ENGINES": "swing=/a/b, day=/c/d"})
    assert targets == [EngineTarget("swing", Path("/a/b")), EngineTarget("day", Path("/c/d"))]


def test_a_malformed_engine_entry_is_a_startup_error():
    with pytest.raises(BotConfigError, match="name=/path"):
        engines_from_env({"TRD_BOT_ENGINES": "swing"})


def test_a_username_in_the_allowlist_is_rejected():
    with pytest.raises(BotConfigError, match="numeric"):
        allowed_users_from_env({"TRD_BOT_ALLOWED_USER_IDS": "silverbeer"})


def test_allowlist_accepts_commas_or_spaces():
    assert allowed_users_from_env({"TRD_BOT_ALLOWED_USER_IDS": "1, 2 3"}) == {1, 2, 3}


# ------------------------------------------------------- applying the queue


def bars(n: int, base: float = 100.0) -> list[DailyBar]:
    # Ending today, not at a fixed date: backfill_symbol pulls a window measured
    # back from today, so a series anchored in the past lands only partly inside
    # it and the depth these tests assert on would drift with the calendar.
    start = date.today() - timedelta(days=n - 1)
    return [
        DailyBar(
            date=start + timedelta(days=i),
            open=Decimal(str(base + i)),
            high=Decimal(str(base + i + 1)),
            low=Decimal(str(base + i - 1)),
            close=Decimal(str(base + i)),
            volume=1_000_000,
        )
        for i in range(n)
    ]


@pytest.fixture
def queue_env(conn: duckdb.DuckDBPyConnection, provider: FakeProvider, tmp_path: Path):
    provider.add_symbol("AAA", price="100")
    provider.add_bars("AAA", bars(300))
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA"])
    home = tmp_path / "engine"
    home.mkdir()
    return CommandQueueService(conn, provider), engine, home, provider


def test_applying_an_add_puts_the_symbol_in_the_universe(queue_env):
    service, engine, home, provider = queue_env
    provider.add_symbol("PLTR", price="50")
    provider.add_bars("PLTR", bars(300, base=40))

    enqueue(home, update_id=1, kind="add", symbol="PLTR", user_id=ME)
    applied = service.drain(home)

    assert [a.ok for a in applied] == [True]
    assert "ready to trade" in applied[0].message
    assert "PLTR" in [i.symbol for i in engine.universe()]


def test_a_thin_symbol_is_added_but_reported_as_not_tradable(queue_env):
    """Bar depth is the whole point of confirming an add. A name with 12 bars is
    in the universe and invisible to every rule — silence there reads as a broken
    engine rather than one that is warming up."""
    service, engine, home, provider = queue_env
    provider.add_symbol("THIN", price="10")
    provider.add_bars("THIN", bars(12, base=10))

    enqueue(home, update_id=1, kind="add", symbol="THIN", user_id=ME)
    applied = service.drain(home)

    assert applied[0].ok
    assert "not tradable yet" in applied[0].message.lower()
    assert "THIN" in [i.symbol for i in engine.universe()]


def test_adding_a_symbol_already_in_the_universe_is_not_an_error(queue_env):
    service, _engine, home, _provider = queue_env
    enqueue(home, update_id=1, kind="add", symbol="AAA", user_id=ME)
    applied = service.drain(home)
    assert applied[0].ok
    assert "already" in applied[0].message


def test_an_unknown_symbol_fails_the_command_without_stopping_the_queue(queue_env):
    service, engine, home, provider = queue_env
    provider.add_symbol("GOOD", price="20")
    provider.add_bars("GOOD", bars(300, base=20))

    enqueue(home, update_id=1, kind="add", symbol="NOTREAL", user_id=ME)
    enqueue(home, update_id=2, kind="add", symbol="GOOD", user_id=ME)
    applied = service.drain(home)

    assert [a.ok for a in applied] == [False, True]
    assert "GOOD" in [i.symbol for i in engine.universe()]


def test_removing_a_symbol_leaves_an_open_position_running(queue_env, conn):
    """Dropping a name stops new entries. The trade already on keeps its stop,
    its target and its exit rules — the engine closes it, not the chat."""
    service, engine, home, _provider = queue_env
    # Opened through the repo rather than by waiting for a strategy to fire: what
    # is under test is what removal does to a held name, not which rule bought it.
    account = engine.account()
    instrument = InstrumentRepo(conn).get_by_symbol("AAA")
    assert instrument is not None
    EnginePositionRepo(conn).open(
        account_id=account.id,
        instrument_id=instrument.id,
        signal_id=None,
        strategy="momentum",
        opened_at=datetime.now(),
        entry_price=Decimal("100"),
        quantity=Decimal("10"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        atr_at_entry=Decimal("2.5"),
        last_bar_date=date.today(),
    )

    enqueue(home, update_id=1, kind="rm", symbol="AAA", user_id=ME)
    applied = service.drain(home)

    assert applied[0].ok
    assert "open position stays" in applied[0].message
    assert "AAA" not in [i.symbol for i in engine.universe()]
    assert "AAA" in {row.instrument.symbol for row in engine.position_rows(open_only=True)}


def test_removing_a_symbol_that_is_not_there_reports_the_failure(queue_env):
    service, _engine, home, _provider = queue_env
    enqueue(home, update_id=1, kind="rm", symbol="ZZZ", user_id=ME)
    applied = service.drain(home)
    assert applied[0].ok is False
    assert "not on watchlist" in applied[0].message


def test_a_drained_command_is_not_applied_twice(queue_env):
    """Telegram retains unacked updates for 24h, so a redelivery is normal.
    Applying one twice is not."""
    service, _engine, home, provider = queue_env
    provider.add_symbol("PLTR", price="50")
    provider.add_bars("PLTR", bars(300, base=40))

    enqueue(home, update_id=1, kind="add", symbol="PLTR", user_id=ME)
    assert len(service.drain(home)) == 1

    enqueue(home, update_id=1, kind="add", symbol="PLTR", user_id=ME)  # redelivered
    assert service.drain(home) == []


def test_an_empty_queue_drains_to_nothing(queue_env):
    service, _engine, home, _provider = queue_env
    assert service.drain(home) == []


def test_queued_commands_round_trip_through_json(tmp_path):
    home = tmp_path / "engine"
    home.mkdir()
    path = enqueue(home, update_id=5, kind="add", symbol="pltr", user_id=ME)
    loaded = QueuedCommand.model_validate_json(path.read_text())
    assert loaded.symbol == "PLTR"  # normalized on the way in
    assert loaded.user_id == ME
