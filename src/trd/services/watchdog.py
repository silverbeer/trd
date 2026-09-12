"""Does the engine still have a pulse — asked from outside the engine.

The engine went dark twice in three days (2026-09-09 and 2026-09-11, SB-1054)
and nothing said so. Both times k3s was healthy and the trading rules were
fine: what broke was the host's channel into the VM, which carries the
filesystem share the pods read their databases through. The pods kept starting
every five minutes against an empty directory, the bot crash-looped, and the
first anyone knew was that a Telegram message never arrived.

So this asks the one question none of that could answer: *has a scan landed
recently?* And it asks it from the host, on a timer of its own, because a
watchdog inside the thing it is watching learns nothing when that thing stops.

Two rules shape it.

**It never opens a database.** It reads `status.json`, the snapshot every scan
publishes. That is the same discipline the Telegram bot follows and for the
same reason — DuckDB has one writer, and a monitor that can block a scan is a
liability rather than a safeguard. It also means a missing or stale file is
itself the signal: when the share broke, the pods wrote nothing and the file
simply stopped moving.

**It only complains when silence is wrong.** Outside market hours, on a
weekend, on a holiday, no scan is expected and no alert is due. A monitor that
cries on every quiet evening is one nobody reads by the second week.
"""

from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, computed_field

MARKET_TZ = ZoneInfo("America/New_York")

# The session, as clock minutes. Scans run 09:30-16:00; the first one lands a
# minute or so after the bell, so the window opens with a little slack.
SESSION_OPEN = 935
SESSION_CLOSE = 1600

# How long a silence has to last before it means something. Scans are every five
# minutes, so fifteen is three missed passes — long enough that one slow yfinance
# call or a node hiccup is not an incident, short enough to catch the real thing
# inside a quarter of an hour rather than nineteen hours.
DEFAULT_MAX_AGE_MINUTES = 15

# How often to repeat an alert that is still true. The first one is the useful
# one; this exists so a multi-hour outage does not send forty messages.
DEFAULT_REPEAT_MINUTES = 60

STATE_FILENAME = ".watchdog.json"
STATUS_FILENAME = "status.json"


def in_session(moment: datetime) -> bool:
    """Whether a scan is expected right now.

    Weekday and inside the bell, in exchange-local time. Market holidays are not
    filtered: the cost of that is at most one wrong alert a year, and the cost of
    a calendar that silently goes stale is missing a real outage on a day it
    claims is a holiday.

    A naive timestamp is read as already being exchange-local, never as the
    machine's own zone. That is the convention the rest of the engine writes in —
    `last_scan` is a naive local stamp — and assuming the host clock instead made
    this answer "closed" all afternoon on any box running UTC, which is every CI
    runner and any cluster node outside New York.
    """
    local = moment.astimezone(MARKET_TZ) if moment.tzinfo else moment
    if local.weekday() > 4:
        return False
    return SESSION_OPEN <= local.hour * 100 + local.minute <= SESSION_CLOSE


class EnginePulse(BaseModel):
    """One engine's answer, and what was read to get it."""

    engine: str
    home: str
    last_scan: datetime | None = None
    age_minutes: int | None = None
    problem: str | None = None  # None when the engine is fine

    @computed_field  # type: ignore[prop-decorator]
    @property
    def alive(self) -> bool:
        return self.problem is None


class WatchdogResult(BaseModel):
    """Every engine's pulse, and whether anything should be said about it."""

    at: datetime
    in_session: bool
    max_age_minutes: int
    engines: list[EnginePulse] = []

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failing(self) -> list[str]:
        return [e.engine for e in self.engines if not e.alive]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def healthy(self) -> bool:
        return not self.failing


def read_pulse(engine: str, home: Path, now: datetime, max_age: int) -> EnginePulse:
    """One engine, judged on the snapshot its last scan published.

    Every failure mode is named rather than collapsed into "unhealthy", because
    the fix differs: a missing file is a mount or a deploy, a stale one is an
    engine that stopped, an unreadable one is a half-written file.
    """
    status = home / STATUS_FILENAME
    pulse = EnginePulse(engine=engine, home=str(home))
    if not status.exists():
        pulse.problem = (
            f"no {STATUS_FILENAME} in {home} — the engine has never published, or the mount is gone"
        )
        return pulse
    try:
        payload = json.loads(status.read_text())
    except (OSError, ValueError) as exc:
        pulse.problem = f"{STATUS_FILENAME} could not be read: {exc}"
        return pulse

    raw = payload.get("last_scan")
    if not raw:
        pulse.problem = "no scan has ever been recorded"
        return pulse
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        pulse.problem = f"last_scan is not a timestamp: {raw!r}"
        return pulse

    pulse.last_scan = last
    # Both sides naive-local, which is what the engine writes and what the host
    # runs on. Comparing a naive stamp against an aware one raises, and a
    # watchdog that crashes is worse than one that is slightly imprecise.
    reference = now.astimezone(MARKET_TZ).replace(tzinfo=None) if now.tzinfo else now
    pulse.age_minutes = max(0, int((reference - last).total_seconds() // 60))
    if pulse.age_minutes > max_age:
        pulse.problem = (
            f"last scan was {pulse.age_minutes} minutes ago "
            f"({last:%a %b %-d %H:%M}) — more than the {max_age} allowed"
        )
    return pulse


def check(
    targets: list[tuple[str, Path]],
    now: datetime | None = None,
    max_age: int = DEFAULT_MAX_AGE_MINUTES,
) -> WatchdogResult:
    """Ask every engine for a pulse. Pure: files in, a verdict out."""
    now = now or datetime.now()
    session = in_session(now)
    return WatchdogResult(
        at=now,
        in_session=session,
        max_age_minutes=max_age,
        engines=[read_pulse(name, home, now, max_age) for name, home in targets],
    )


def alert_message(result: WatchdogResult) -> str:
    """What to say when the engine has stopped. Blunt, and it names the check."""
    lines = ["🔴 trd engine is not scanning"]
    for pulse in result.engines:
        mark = "✗" if not pulse.alive else "✓"
        detail = pulse.problem or (
            f"last scan {pulse.age_minutes} minutes ago" if pulse.age_minutes is not None else "ok"
        )
        lines.append(f"{mark} {pulse.engine}: {detail}")
    lines += [
        "",
        "The market is open and no scan has landed. Open positions are unmanaged: "
        "stops are only honoured by a scan.",
        "Check the cluster first — 'kubectl get nodes'. Twice in September the VM "
        "was healthy and only the host's channel into it had died, which empties "
        "the engine homes and every pod then runs against nothing.",
    ]
    return "\n".join(lines)


def recovery_message(result: WatchdogResult) -> str:
    lines = ["🟢 trd engine is scanning again"]
    for pulse in result.engines:
        if pulse.age_minutes is not None:
            lines.append(f"✓ {pulse.engine}: last scan {pulse.age_minutes} minutes ago")
    return "\n".join(lines)


# ------------------------------------------------------------------- the state
#
# A file, not a database: the watchdog must keep working when the databases are
# exactly what is unreachable.


class WatchdogState(BaseModel):
    """What was said last time, so a long outage is not forty messages."""

    alerting: bool = False
    last_alert_at: datetime | None = None

    @classmethod
    def load(cls, path: Path) -> WatchdogState:
        try:
            return cls.model_validate_json(path.read_text())
        except (OSError, ValueError):
            return cls()

    def save(self, path: Path) -> None:
        # A watchdog that cannot write its own state should still alert; it will
        # simply repeat itself. Losing the alert is the worse failure.
        with contextlib.suppress(OSError):
            path.write_text(self.model_dump_json())


def decide(
    result: WatchdogResult,
    state: WatchdogState,
    repeat_minutes: int = DEFAULT_REPEAT_MINUTES,
) -> tuple[str | None, WatchdogState]:
    """What to send, if anything, and the state to carry forward.

    Silence outside the session is correct and is never reported. A recovery is
    only announced to someone who was told about the outage.
    """
    now = result.at
    if result.healthy:
        if state.alerting:
            return recovery_message(result), WatchdogState(alerting=False, last_alert_at=now)
        return None, state
    if not result.in_session:
        # Out of hours a stopped engine is not news. It becomes news at 09:35.
        return None, state
    if (
        state.alerting
        and state.last_alert_at is not None
        and now - state.last_alert_at < timedelta(minutes=repeat_minutes)
    ):
        return None, state
    return alert_message(result), WatchdogState(alerting=True, last_alert_at=now)


def session_of(moment: datetime) -> date:
    """The trading date a timestamp belongs to, in exchange-local time."""
    local = moment.astimezone(MARKET_TZ) if moment.tzinfo else moment
    return local.date()
