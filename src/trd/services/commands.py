"""The queue between the chat bot and the engine.

The bot is resident and the scan is not, and DuckDB has one writer. So the bot
never opens the database: it drops an intent here, and the next scan applies it.
That keeps the engine the only writer, and it costs one scan interval of latency
on a command whose effect — a name entering a universe — is measured in days.

The queue is a directory of small JSON files rather than a table, for the same
reason: a table would need a writer.

File naming carries two guarantees. The name is the zero-padded Telegram
`update_id`, which is monotonic, so sorting by filename replays commands in the
order they were typed. And applying moves the file into `done/` under the same
name, so a redelivered update (Telegram retains unacked ones for 24h) is
recognised and skipped rather than applied twice.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import duckdb
from pydantic import BaseModel

from trd.errors import TrdError
from trd.providers.base import MarketDataProvider
from trd.services.engine import EngineService
from trd.services.sync import SyncService
from trd.services.watchlist import WatchlistService

QUEUE_DIR = "commands"
DONE_DIR = "done"

# Two years of daily bars behind a newly added name. The strategies need a couple
# of hundred bars before they will look at a symbol at all, so an add that only
# gap-fills the last week produces a universe member no rule can trade — which
# looks exactly like a rule that never fires.
BACKFILL_YEARS = 2


class CommandKind:
    ADD = "add"
    REMOVE = "rm"


class QueuedCommand(BaseModel):
    """One instruction, as written by the bot and read by the scan."""

    update_id: int
    kind: str
    symbol: str
    user_id: int
    queued_at: datetime


@dataclass(frozen=True)
class AppliedCommand:
    """What happened, in a sentence the bot can send back verbatim."""

    command: QueuedCommand
    ok: bool
    message: str


def _queue_path(home: Path) -> Path:
    return home / QUEUE_DIR


def enqueue(
    home: Path,
    update_id: int,
    kind: str,
    symbol: str,
    user_id: int,
    now: datetime | None = None,
) -> Path:
    """Write one intent for `home`'s engine to apply. Atomic: tmp then rename, so
    a scan reading the directory mid-write never sees a half-written file."""
    directory = _queue_path(home)
    directory.mkdir(parents=True, exist_ok=True)
    command = QueuedCommand(
        update_id=update_id,
        kind=kind,
        symbol=symbol.upper(),
        user_id=user_id,
        queued_at=now or datetime.now(),
    )
    path = directory / f"{update_id:020d}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(command.model_dump_json())
    tmp.replace(path)
    return path


def pending(home: Path) -> list[QueuedCommand]:
    """Queued commands in the order they were typed."""
    directory = _queue_path(home)
    if not directory.is_dir():
        return []
    out: list[QueuedCommand] = []
    for path in sorted(directory.glob("*.json")):
        try:
            out.append(QueuedCommand.model_validate_json(path.read_text()))
        except (OSError, ValueError):
            # An unparseable file is retired, not retried: leaving it would block
            # the queue behind it on every scan, forever.
            _retire(path)
    return out


def _retire(path: Path) -> None:
    done = path.parent / DONE_DIR
    done.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.replace(done / path.name)


class CommandQueueService:
    """Applies queued chat commands. Runs inside the scan's process, which is the
    only thing holding the database open."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.engine = EngineService(conn, provider)
        self.watchlists = WatchlistService(conn, provider)
        self.sync = SyncService(conn, provider)

    def drain(self, home: Path) -> list[AppliedCommand]:
        results: list[AppliedCommand] = []
        directory = _queue_path(home)
        for command in pending(home):
            path = directory / f"{command.update_id:020d}.json"
            if (directory / DONE_DIR / path.name).exists():
                # Already applied on an earlier pass and redelivered since.
                _retire(path)
                continue
            results.append(self.apply(command))
            _retire(path)
        return results

    def apply(self, command: QueuedCommand) -> AppliedCommand:
        try:
            if command.kind == CommandKind.ADD:
                return AppliedCommand(command, True, self._add(command.symbol))
            if command.kind == CommandKind.REMOVE:
                return AppliedCommand(command, True, self._remove(command.symbol))
            return AppliedCommand(command, False, f"unknown command {command.kind!r}")
        except TrdError as exc:
            return AppliedCommand(command, False, str(exc))

    # ------------------------------------------------------------- actions

    def _add(self, symbol: str) -> str:
        config = self.engine.config()
        added = self.watchlists.add(symbol, config.watchlist)

        # Backfill before reporting. A name is only really in the universe once
        # it has the history its rules need, and saying "added" while the scan
        # silently skips it is worse than an error — the engine looks broken
        # rather than warming up.
        bars = self.sync.backfill_symbol(symbol, years=BACKFILL_YEARS)
        status = self.engine.status()
        short = dict(status.short_history)
        depth = short.get(symbol)
        where = f"{config.watchlist} ({status.account})"
        prefix = f"added {symbol} to {where}" if added else f"{symbol} was already on {where}"
        if depth is not None:
            return (
                f"{prefix} — {depth} {status.bar_unit} bars, below the "
                f"{status.warmup_bars} its rules need. Not tradable yet; "
                f"the daily sync will keep filling it in."
            )
        return f"{prefix} — {bars} bars pulled, ready to trade."

    def _remove(self, symbol: str) -> str:
        config = self.engine.config()
        # Deliberately does not touch an open position. Dropping a name from the
        # universe stops new entries; the trade already on keeps its stop, its
        # target and its exit rules, and closes on the rules that opened it.
        # Straight from the repo rather than position_rows(): that would fetch
        # quotes to mark the book, and a removal has no use for a live price.
        account = self.engine.account()
        held = {i.symbol for _, i in self.engine.positions.list_open(account.id)}
        self.watchlists.remove(symbol, config.watchlist)
        note = f"removed {symbol} from {config.watchlist}"
        if symbol in held:
            return f"{note} — the open position stays and will close on its own exit rules."
        return note
