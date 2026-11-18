"""Keep asking about a live exit nobody has acted on (SB-1735).

On 2026-11-17 the live engine's stop rule fired at 09:55. The runner placed the
sell, the broker held it for a trade approval, and the push that should have
reached the owner's phone never did. The runner read the approval back every ten
minutes and saw PENDING thirty-seven times, which it logged exactly like an idle
pass. At 16:00 the approval lapsed and the position stayed open overnight, below
its stop. Nothing had said a word.

A buy that waits costs an opportunity. An exit that waits leaves a position
running past the rule that said to close it, so an exit gets asked about again
and again until it is answered: first after `AFTER_MINUTES`, then every
`EVERY_MINUTES`.

Read-only by connection. What was already said lives in a small file in the
engine home, not the database, for the reason the watchdog keeps its state in
one: this must never take the writer lock a scan needs.
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime, time, timedelta
from pathlib import Path

import duckdb
from pydantic import BaseModel

from trd.models import WaitingExit
from trd.repos import AccountRepo
from trd.repos.engine import EngineConfigRepo, OrderIntentRepo
from trd.services.fills import live_engine
from trd.services.guard import waiting_exits

AFTER_MINUTES = 15
EVERY_MINUTES = 30
STATE_FILENAME = ".exit-nudges.json"

# An exit decided before the bell cannot reach the broker until the session
# opens, so its wait is counted from the open — not from 07:41.
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)


class ExitNudge(BaseModel):
    """One exit that is owed a reminder now."""

    waiting: WaitingExit
    minutes: int


class NudgeResult(BaseModel):
    at: datetime
    in_session: bool
    waiting: list[WaitingExit] = []
    due: list[ExitNudge] = []
    sent: list[int] = []  # intent ids actually delivered (with --notify)


def minutes_waiting(waiting: WaitingExit, now: datetime) -> int:
    """Minutes since the exit could first have been acted on: when it reached
    the broker, or the session open if it was decided before the bell."""
    opened = datetime.combine(now.date(), SESSION_OPEN)
    start = max(waiting.since, opened)
    return max(0, int((now - start).total_seconds() // 60))


def in_session(now: datetime) -> bool:
    return now.weekday() < 5 and SESSION_OPEN <= now.time() <= SESSION_CLOSE


def due_nudges(
    waiting: list[WaitingExit],
    now: datetime,
    last_sent: dict[int, datetime],
    after: int = AFTER_MINUTES,
    every: int = EVERY_MINUTES,
) -> list[ExitNudge]:
    """Which waiting exits to remind about now. Pure.

    Nothing outside the session: no approval can be answered then, and an exit
    decided pre-market is the runner's to place at the open.
    """
    if not in_session(now):
        return []
    due = []
    for exit_ in waiting:
        minutes = minutes_waiting(exit_, now)
        if minutes < after:
            continue
        previous = last_sent.get(exit_.intent_id)
        if previous is not None and now - previous < timedelta(minutes=every):
            continue
        due.append(ExitNudge(waiting=exit_, minutes=minutes))
    return due


def load_state(path: Path) -> dict[int, datetime]:
    try:
        raw = json.loads(path.read_text())
        return {int(k): datetime.fromisoformat(v) for k, v in raw.items()}
    except (OSError, ValueError, AttributeError, TypeError):
        return {}


def save_state(path: Path, state: dict[int, datetime], still_waiting: set[int]) -> None:
    """Forget exits that stopped waiting, so the file never grows."""
    kept = {str(k): v.isoformat() for k, v in state.items() if k in still_waiting}
    with contextlib.suppress(OSError):
        path.write_text(json.dumps(kept))


class ExitNudgeService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.configs = EngineConfigRepo(conn)
        self.accounts = AccountRepo(conn)
        self.intents = OrderIntentRepo(conn)

    def waiting(self) -> list[WaitingExit]:
        _, account = live_engine(
            self.configs,
            self.accounts,
            "This is a paper engine: it fills its own exits, so none can be left waiting. "
            "'trd engine nudge' is for an engine created with --live.",
        )
        return waiting_exits(self.intents.list_open(account.id))
