"""A Telegram bot that drives the engines from chat.

The engine already pushes fills to a channel. This makes the channel two-way:
"add PLTR to the day and swing engines" becomes a message rather than an ssh
session.

Three things decide the shape of this module.

**Long polling, not a webhook.** A webhook needs a public HTTPS endpoint with a
valid certificate. The engines run on a mini behind NAT, so the bot goes and
fetches instead: `getUpdates` holds the connection open for a few seconds and
returns whatever arrived. Outbound 443, same host the notifier already talks to.

**The bot never opens DuckDB.** It is resident; the scan CronJob is not. A
writer held open by a chat feature would lock out a scan, which puts Telegram in
the trading path. So reads answer from the files the scan entrypoint already
publishes, and writes are queued as intents for the next scan to apply. There is
still exactly one writer, and it is still the engine.

**One database is one engine.** `EngineConfigRepo.get()` takes the most recent
config, so the day engine and the swing engine are separate TRD_HOME directories
with separate CronJobs. A command naming both writes two queue files, one under
each engine's home, and each engine drains only its own.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from trd.errors import NotifyError, TrdError
from trd.services.commands import QUEUE_DIR, CommandKind, enqueue

API_ROOT = "https://api.telegram.org"

# Telegram rejects anything longer outright; chunking beats a dropped reply.
MAX_MESSAGE = 4096

# How long Telegram holds an empty getUpdates open. Long enough that idling costs
# ~2 requests a minute, short enough that a rolling restart is not left hanging.
POLL_TIMEOUT = 25

# A symbol, not free text: what reaches the queue becomes a provider lookup and a
# watchlist row. Letters, digits, dot and dash — enough for BRK.B and BTC-USD.
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class BotConfigError(TrdError):
    """The bot cannot start — a missing token, or no engines to talk to."""


@dataclass(frozen=True)
class EngineTarget:
    """One engine the bot can drive: a name to type in chat, and its TRD_HOME."""

    name: str
    home: Path


@dataclass(frozen=True)
class Reply:
    """What to say back. `chunks` because Telegram caps a message at 4096."""

    text: str

    def chunks(self) -> list[str]:
        return [self.text[i : i + MAX_MESSAGE] for i in range(0, len(self.text), MAX_MESSAGE)] or [
            ""
        ]


class Transport(Protocol):
    """The Telegram Bot API, narrowed to the two calls this needs.

    Same reasoning as MarketDataProvider: one small interface, a real
    implementation over urllib, and a fake in the tests so the suite never
    touches the network.
    """

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]: ...

    def send(self, chat_id: str, text: str) -> None: ...

    def set_commands(self, commands: list[tuple[str, str]]) -> None: ...


class TelegramTransport:
    """Bot API over stdlib urllib — same dependency-free choice as the notifier."""

    def __init__(self, token: str, api_root: str = API_ROOT) -> None:
        self.token = token
        self.api_root = api_root

    def _call(self, method: str, payload: dict[str, Any], timeout: int) -> Any:
        request = urllib.request.Request(
            f"{self.api_root}/bot{self.token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            # 409 is the one worth naming: two pollers on one token, which is a
            # deployment mistake (replicas > 1, or a RollingUpdate overlap) and
            # not a transient failure. The token is in the URL — never log exc.url.
            if exc.code == 409:
                raise NotifyError(
                    "Telegram 409 Conflict: another process is polling this bot token. "
                    "Only one poller may run (check replicas and the update strategy)."
                ) from None
            raise NotifyError(f"Telegram rejected {method} (HTTP {exc.code}).") from None
        except urllib.error.URLError as exc:
            raise NotifyError(f"Could not reach Telegram: {exc.reason}") from None
        if not body.get("ok"):
            raise NotifyError(f"Telegram refused {method}: {body.get('description', 'unknown')}")
        return body.get("result")

    def get_updates(self, offset: int, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            # Only messages. Edits, callbacks and channel posts are dropped by
            # Telegram before they reach us rather than being parsed and ignored.
            "allowed_updates": ["message"],
        }
        if offset:
            payload["offset"] = offset
        # Socket timeout must outlast the long poll or every idle poll raises.
        result = self._call("getUpdates", payload, timeout=timeout + 10)
        return list(result or [])

    def send(self, chat_id: str, text: str) -> None:
        # No parse_mode, for the reason TelegramNotifier documents: reasons carry
        # %, parentheses and em-dashes, and legacy Markdown rejects the whole
        # message on one unmatched character.
        self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self._call(
            "setMyCommands",
            {"commands": [{"command": c, "description": d} for c, d in commands]},
            timeout=15,
        )


# --------------------------------------------------------------- parsing

MENU = [
    ("add", "Add a symbol to one or more engine universes"),
    ("rm", "Remove a symbol from one or more engine universes"),
    ("status", "Build, capacity, bar depth, P&L, money at risk"),
    ("book", "The open book and the strategy scorecard"),
    ("report", "Per-strategy expectancy over closed trades"),
    ("engines", "Which engines this bot can drive"),
    ("help", "This list"),
]

HELP = "\n".join(
    [
        "trd engine bot — commands",
        "",
        "/add SYM [engine ...]   queue a universe add (all engines if none named)",
        "/rm SYM [engine ...]    queue a universe removal",
        "/status [engine]        build, capacity, bar depth, P&L, risk",
        "/book [engine]          open positions + scorecard, as last published",
        "/report [engine]        per-strategy expectancy",
        "/engines                list the engines this bot drives",
        "",
        "Writes are queued and applied by the next scan (<=5 min during the",
        "session). Reads answer from the last published snapshot, so they are",
        "as fresh as the last scan.",
        "",
        "There is deliberately no order placement here: the engines trade",
        "simulation accounts and entries/exits stay with the rules.",
    ]
)


@dataclass(frozen=True)
class Command:
    """A parsed, validated instruction. `engines` is always resolved to real
    targets — an unknown name is rejected at parse time, not at apply time."""

    kind: str
    engines: list[EngineTarget]
    symbol: str | None = None


class ParseError(TrdError):
    """The message was addressed to us but is not a command we can run."""


def parse(text: str, engines: list[EngineTarget]) -> Command:
    """Turn message text into a Command, or raise ParseError with what to type.

    Bare text (no leading slash) is accepted for the read commands so "status"
    works as well as "/status"; writes require the slash, because a symbol and a
    verb are easy to type by accident.
    """
    words = text.strip().split()
    if not words:
        raise ParseError("Empty message. Try /help.")

    # In a group Telegram appends @botname to commands. Harmless here, but strip
    # it so /status@trdbot is not a different command from /status.
    verb = words[0].split("@", 1)[0].lstrip("/").lower()
    rest = words[1:]

    if verb in ("help", "start"):
        return Command(kind="help", engines=[])
    if verb == "engines":
        return Command(kind="engines", engines=engines)

    if verb in ("status", "book", "positions", "report"):
        kind = "book" if verb == "positions" else verb
        return Command(kind=kind, engines=_targets(rest, engines))

    if verb in ("add", "rm", "remove"):
        if not rest:
            raise ParseError(f"/{verb} needs a symbol, e.g. /{verb} PLTR day swing")
        symbol = rest[0].upper()
        if not SYMBOL.match(symbol):
            raise ParseError(f"{rest[0]!r} does not look like a symbol.")
        return Command(
            kind=CommandKind.ADD if verb == "add" else CommandKind.REMOVE,
            engines=_targets(rest[1:], engines),
            symbol=symbol,
        )

    raise ParseError(f"Unknown command {words[0]!r}. Try /help.")


def _targets(names: list[str], engines: list[EngineTarget]) -> list[EngineTarget]:
    """Resolve engine names, defaulting to every engine.

    Defaulting to all is the right bias for this bot: the universes are meant to
    agree, and the common case really is "put this name in front of both sets of
    rules". Naming one is the exception, so it is the thing you have to type.
    """
    if not names:
        return list(engines)
    known = {e.name.lower(): e for e in engines}
    out: list[EngineTarget] = []
    for name in names:
        target = known.get(name.lower())
        if target is None:
            raise ParseError(
                f"Unknown engine {name!r}. Known: {', '.join(e.name for e in engines)}."
            )
        if target not in out:
            out.append(target)
    return out


# --------------------------------------------------------------- handling


def format_status(payload: dict[str, Any]) -> str:
    """A `trd engine status --json` document as a few lines worth reading on a
    phone. Realized / unrealized / net together, never the total alone."""
    universe = payload.get("universe") or []
    short = payload.get("short_history") or []
    lines = [
        f"{payload.get('account', '?')} · {payload.get('timeframe', '?')} "
        f"· build {payload.get('build', '?')}",
        f"positions {payload.get('open_positions', 0)}/{payload.get('max_positions', 0)}"
        f" · universe {len(universe)}"
        f" · closed {payload.get('closed_trades', 0)}",
        f"realized {_money(payload.get('realized'))}"
        f" · unrealized {_money(payload.get('unrealized'))}"
        f" · NET {_money(payload.get('net_pnl'))}",
        f"committed {_money(payload.get('committed'))}"
        f" · at risk {_money(payload.get('risk_at_stop'))}",
        f"bars {payload.get('bars_total', 0)} through {payload.get('bars_last') or '—'}"
        f" · last scan {payload.get('last_scan') or 'never'}",
    ]
    if short:
        names = ", ".join(f"{row[0]}({row[1]})" for row in short[:8])
        lines.append(f"⚠ below warm-up, not tradable yet: {names}")
    if payload.get("config_refused"):
        lines.append("⚠ this config is one 'engine init' would refuse — see trd engine status")
    return "\n".join(lines)


def _money(value: Any) -> str:
    if value is None:
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{amount:+,.2f}"


def read_snapshot(home: Path, name: str) -> str | None:
    """A file the scan entrypoint published, or None if it never has.

    Reading is the whole point: these files exist so something can see the engine
    without opening its database.
    """
    path = home / name
    try:
        return path.read_text()
    except OSError:
        return None


class CommandBot:
    """Poll, authorize, act, reply. Owns no database and no market data."""

    def __init__(
        self,
        transport: Transport,
        engines: list[EngineTarget],
        allowed_user_ids: set[int],
        state_dir: Path,
    ) -> None:
        if not engines:
            raise BotConfigError(
                "No engines configured. Set TRD_BOT_ENGINES, e.g. "
                "'swing=/engines/swing,day=/engines/day'."
            )
        if not allowed_user_ids:
            raise BotConfigError(
                "No authorized users. Set TRD_BOT_ALLOWED_USER_IDS to your numeric "
                "Telegram user id — an unrestricted bot would take commands from anyone."
            )
        self.transport = transport
        self.engines = engines
        self.allowed_user_ids = allowed_user_ids
        self.state_dir = state_dir
        self.offset_path = state_dir / ".telegram-offset"

    # ------------------------------------------------------------ offset

    def offset(self) -> int:
        """The last acked update_id + 1, persisted across restarts.

        Telegram redelivers anything unacked for 24h. Without this file a pod
        restart replays a day of commands — the queue's update_id keying makes
        that harmless, but a chat full of duplicate confirmations is its own bug.
        """
        try:
            return int(self.offset_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    def _remember(self, offset: int) -> None:
        tmp = self.offset_path.with_suffix(".tmp")
        tmp.write_text(str(offset))
        tmp.replace(self.offset_path)

    # ------------------------------------------------------------- loop

    def poll_once(self, timeout: int = POLL_TIMEOUT) -> int:
        """One getUpdates round trip. Returns how many updates were handled."""
        updates = self.transport.get_updates(offset=self.offset(), timeout=timeout)
        handled = 0
        for update in updates:
            try:
                self.handle(update)
            finally:
                # Ack even on failure. A message that crashes the handler would
                # otherwise be redelivered forever and the bot would never move
                # past it — a poison pill that takes the whole channel down.
                self._remember(int(update["update_id"]) + 1)
            handled += 1
        return handled

    def serve(self, passes: int | None = None, timeout: int = POLL_TIMEOUT) -> None:
        """Poll until stopped. `passes` bounds it for tests and smoke runs."""
        self.transport.set_commands(MENU)
        done = 0
        while passes is None or done < passes:
            try:
                self.poll_once(timeout=timeout)
            except NotifyError as exc:
                if "409" in str(exc):
                    # A second poller is a deployment fault, not weather. Crash so
                    # the pod restarts visibly rather than looping in silence.
                    raise
                time.sleep(5)
            done += 1

    # ---------------------------------------------------------- handling

    def handle(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text") or ""

        # Authorization, in this order and before anything else touches `text`.
        #
        # The numeric user id, never the username: usernames are user-changeable
        # and, once released, re-registerable by somebody else. `chat.type` must
        # be private — a channel post carries no reliable sender, so there would
        # be nobody to authorize, and the fills channel must stay one-way.
        if chat.get("type") != "private":
            return
        raw_id = sender.get("id")
        if raw_id is None:
            return
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            return
        if user_id not in self.allowed_user_ids:
            # Silently. A refusal tells a scanner the bot is live and worth
            # working on; silence is indistinguishable from a dead token.
            return
        if not text.strip():
            return

        chat_id = str(chat.get("id"))
        try:
            command = parse(text, self.engines)
        except ParseError as exc:
            self._reply(chat_id, str(exc))
            return

        try:
            reply = self.run(command, update_id=int(update["update_id"]), user_id=user_id)
        except TrdError as exc:
            self._reply(chat_id, f"failed: {exc}")
            return
        self._reply(chat_id, reply.text)

    def run(self, command: Command, update_id: int, user_id: int) -> Reply:
        if command.kind == "help":
            return Reply(HELP)
        if command.kind == "engines":
            return Reply(
                "engines this bot drives:\n"
                + "\n".join(f"  {e.name}  ({e.home})" for e in command.engines)
            )
        if command.kind == "status":
            return Reply(self._status(command.engines))
        if command.kind == "book":
            return Reply(self._published(command.engines, "status.txt"))
        if command.kind == "report":
            return Reply(self._report(command.engines))
        return self._queue(command, update_id=update_id, user_id=user_id)

    # ------------------------------------------------------------ reads

    def _status(self, engines: list[EngineTarget]) -> str:
        blocks = []
        for engine in engines:
            raw = read_snapshot(engine.home, "status.json")
            if raw is None:
                blocks.append(f"[{engine.name}] no snapshot yet — has a scan run?")
                continue
            try:
                blocks.append(f"[{engine.name}]\n{format_status(json.loads(raw))}")
            except (ValueError, KeyError):
                blocks.append(f"[{engine.name}] snapshot unreadable")
        return "\n\n".join(blocks)

    def _report(self, engines: list[EngineTarget]) -> str:
        blocks = []
        for engine in engines:
            raw = read_snapshot(engine.home, "report.json")
            rows = json.loads(raw) if raw else []
            if not rows:
                blocks.append(f"[{engine.name}] no closed trades yet")
                continue
            lines = [f"[{engine.name}]"]
            for row in rows:
                # Expectancy before win rate, in that order on purpose: a rule can
                # win most of its trades and still lose money.
                expectancy = row.get("expectancy_r")
                win = row.get("win_rate")
                parts = [f"{row.get('trades', 0)} trades"]
                parts.append(
                    f"expectancy {float(expectancy):+.2f}R"
                    if expectancy is not None
                    else "no R yet"
                )
                if win is not None:
                    parts.append(f"{float(win):.0f}% win")
                lines.append(f"  {row.get('strategy', '?')}: {', '.join(parts)}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    def _published(self, engines: list[EngineTarget], name: str) -> str:
        blocks = []
        for engine in engines:
            text = read_snapshot(engine.home, name)
            blocks.append(
                f"[{engine.name}]\n{text.strip()}"
                if text
                else f"[{engine.name}] nothing published yet — has a scan run?"
            )
        return "\n\n".join(blocks)

    # ----------------------------------------------------------- writes

    def _queue(self, command: Command, update_id: int, user_id: int) -> Reply:
        assert command.symbol is not None
        written = []
        for engine in command.engines:
            enqueue(
                engine.home,
                update_id=update_id,
                kind=command.kind,
                symbol=command.symbol,
                user_id=user_id,
            )
            written.append(engine.name)
        verb = "add" if command.kind == CommandKind.ADD else "remove"
        return Reply(
            f"queued: {verb} {command.symbol} → {', '.join(written)}\n"
            "applies on the next scan (<=5 min during the session); "
            "you'll get a confirmation here."
        )

    def _reply(self, chat_id: str, text: str) -> None:
        for chunk in Reply(text).chunks():
            try:
                self.transport.send(chat_id, chunk)
            except NotifyError:
                # A reply that will not send must not stop the bot: the command
                # itself already succeeded, and the queue is the durable record.
                return


# ----------------------------------------------------------------- env


def engines_from_env(env: dict[str, str] | None = None) -> list[EngineTarget]:
    """Parse TRD_BOT_ENGINES='swing=/engines/swing,day=/engines/day'.

    Explicit rather than discovered: the bot mounts each engine's home read-write
    only so it can drop queue files there, and a bot that went looking for
    databases could find the real portfolio's.
    """
    source = env if env is not None else dict(os.environ)
    raw = source.get("TRD_BOT_ENGINES", "").strip()
    targets: list[EngineTarget] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, home = chunk.partition("=")
        if not sep or not name.strip() or not home.strip():
            raise BotConfigError(
                f"Bad TRD_BOT_ENGINES entry {chunk!r}. Expected 'name=/path/to/home'."
            )
        targets.append(EngineTarget(name=name.strip(), home=Path(home.strip())))
    return targets


def allowed_users_from_env(env: dict[str, str] | None = None) -> set[int]:
    source = env if env is not None else dict(os.environ)
    raw = source.get("TRD_BOT_ALLOWED_USER_IDS", "")
    out: set[int] = set()
    for chunk in raw.replace(",", " ").split():
        try:
            out.add(int(chunk))
        except ValueError:
            raise BotConfigError(
                f"TRD_BOT_ALLOWED_USER_IDS entry {chunk!r} is not a numeric Telegram user id. "
                "Usernames are not accepted: they are changeable and re-registerable."
            ) from None
    return out


def bot_from_env(env: dict[str, str] | None = None) -> CommandBot:
    source = env if env is not None else dict(os.environ)
    token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise BotConfigError("TELEGRAM_BOT_TOKEN is not set.")
    engines = engines_from_env(source)
    if not engines:
        raise BotConfigError(
            "TRD_BOT_ENGINES is empty. Set it to 'name=/path,name=/path' — one entry "
            "per engine home, since one database is one engine."
        )
    # Defaults beside the first engine's queue rather than in the image: the
    # offset has to outlive the pod, and only the mounted homes do.
    state = Path(source.get("TRD_BOT_STATE", "").strip() or engines[0].home / QUEUE_DIR)
    state.mkdir(parents=True, exist_ok=True)
    return CommandBot(
        transport=TelegramTransport(token),
        engines=engines,
        allowed_user_ids=allowed_users_from_env(source),
        state_dir=state,
    )
