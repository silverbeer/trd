"""The watchdog: does the engine still have a pulse, asked from outside it.

What is worth testing here is not the arithmetic of a timestamp. It is the
judgement around it: that silence out of hours is not an incident, that a long
outage is one message rather than forty, that a recovery is only announced to
someone who heard the alarm, and that every way the snapshot can be missing or
malformed produces a named problem instead of a crash — because a watchdog that
throws is a watchdog that is not watching.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from trd.services.watchdog import (
    DEFAULT_MAX_AGE_MINUTES,
    WatchdogState,
    alert_message,
    check,
    decide,
    in_session,
    read_pulse,
)

# A Wednesday. Chosen once so every "in the session" fixture below is honest
# about the weekday, which is half of what `in_session` decides.
MIDSESSION = datetime(2026, 9, 9, 11, 30)


def home_with(tmp_path: Path, name: str, last_scan: str | None, **extra) -> Path:
    home = tmp_path / name
    home.mkdir()
    payload = {"build": "test", **extra}
    if last_scan is not None:
        payload["last_scan"] = last_scan
    (home / "status.json").write_text(json.dumps(payload))
    return home


# --------------------------------------------------------------- when to speak


def test_silence_is_only_wrong_while_the_market_is_open() -> None:
    """A monitor that cries every evening is one nobody reads by week two."""
    assert in_session(datetime(2026, 9, 9, 11, 30)) is True  # Wednesday, midday
    assert in_session(datetime(2026, 9, 9, 9, 0)) is False  # before the bell
    assert in_session(datetime(2026, 9, 9, 16, 30)) is False  # after the close
    assert in_session(datetime(2026, 9, 12, 11, 30)) is False  # Saturday
    assert in_session(datetime(2026, 9, 13, 11, 30)) is False  # Sunday


def test_an_engine_that_stopped_mid_session_is_a_problem(tmp_path: Path) -> None:
    """The real failure, twice in three days: scans stop and everything else
    looks fine."""
    home = home_with(tmp_path, "day", "2026-09-09T10:10:00")
    result = check([("day", home)], now=MIDSESSION)
    assert result.in_session is True
    assert result.healthy is False
    assert result.failing == ["day"]
    assert "80 minutes ago" in (result.engines[0].problem or "")


def test_a_scan_a_few_minutes_old_is_fine(tmp_path: Path) -> None:
    home = home_with(tmp_path, "day", "2026-09-09T11:25:00")
    result = check([("day", home)], now=MIDSESSION)
    assert result.healthy is True
    assert result.engines[0].age_minutes == 5
    assert result.engines[0].problem is None


def test_out_of_hours_a_stopped_engine_is_not_news(tmp_path: Path) -> None:
    home = home_with(tmp_path, "day", "2026-09-09T10:10:00")
    saturday = datetime(2026, 9, 12, 11, 30)
    result = check([("day", home)], now=saturday)
    assert result.healthy is False  # it IS stale
    assert result.in_session is False  # but nobody should be told
    message, state = decide(result, WatchdogState())
    assert message is None
    assert state.alerting is False


# ------------------------------------------------------- every way it can break


def test_a_missing_snapshot_names_the_mount(tmp_path: Path) -> None:
    """The 2026-09-09 shape exactly: the share died, the directory was empty."""
    empty = tmp_path / "day"
    empty.mkdir()
    pulse = read_pulse("day", empty, MIDSESSION, DEFAULT_MAX_AGE_MINUTES)
    assert pulse.alive is False
    assert "mount" in (pulse.problem or "")


def test_a_half_written_snapshot_does_not_crash_the_watchdog(tmp_path: Path) -> None:
    home = tmp_path / "day"
    home.mkdir()
    (home / "status.json").write_text('{"last_scan": "2026-09-09T1')  # truncated mid-write
    pulse = read_pulse("day", home, MIDSESSION, DEFAULT_MAX_AGE_MINUTES)
    assert pulse.alive is False
    assert "could not be read" in (pulse.problem or "")


def test_a_snapshot_with_no_scan_in_it_is_named_as_such(tmp_path: Path) -> None:
    home = home_with(tmp_path, "day", None)
    pulse = read_pulse("day", home, MIDSESSION, DEFAULT_MAX_AGE_MINUTES)
    assert pulse.alive is False
    assert "no scan has ever been recorded" in (pulse.problem or "")


def test_a_nonsense_timestamp_is_named_rather_than_raised(tmp_path: Path) -> None:
    home = home_with(tmp_path, "day", "not-a-time")
    pulse = read_pulse("day", home, MIDSESSION, DEFAULT_MAX_AGE_MINUTES)
    assert pulse.alive is False
    assert "not a timestamp" in (pulse.problem or "")


# ------------------------------------------------------------ what gets said


def test_a_long_outage_is_one_message_not_forty(tmp_path: Path) -> None:
    """Nineteen hours at five-minute checks is 228 alerts without this."""
    home = home_with(tmp_path, "day", "2026-09-09T10:10:00")
    result = check([("day", home)], now=MIDSESSION)

    first, state = decide(result, WatchdogState())
    assert first is not None and state.alerting is True

    again, state2 = decide(result, state)
    assert again is None  # still failing, already said so

    later = check([("day", home)], now=MIDSESSION + timedelta(hours=2))
    third, _ = decide(later, state2)
    assert third is not None  # two hours on, worth repeating


def test_recovery_is_only_announced_to_someone_who_heard_the_alarm(
    tmp_path: Path,
) -> None:
    home = home_with(tmp_path, "day", "2026-09-09T11:25:00")
    healthy = check([("day", home)], now=MIDSESSION)

    quiet, _ = decide(healthy, WatchdogState())
    assert quiet is None  # never alerted, so nothing to recover from

    recovered, state2 = decide(healthy, WatchdogState(alerting=True, last_alert_at=MIDSESSION))
    assert recovered is not None and "scanning again" in recovered
    assert state2.alerting is False


def test_the_alert_says_what_is_at_stake_and_where_to_look(tmp_path: Path) -> None:
    """An alert that only says "unhealthy" sends its reader hunting. This one
    names the consequence and the first command to run."""
    home = home_with(tmp_path, "day", "2026-09-09T10:10:00")
    text = alert_message(check([("day", home)], now=MIDSESSION))
    assert "not scanning" in text
    assert "unmanaged" in text  # open positions, with stops nothing is honouring
    assert "kubectl get nodes" in text


def test_one_failing_engine_fails_the_check_even_when_the_other_is_fine(
    tmp_path: Path,
) -> None:
    swing = home_with(tmp_path, "swing", "2026-09-09T11:28:00")
    day = home_with(tmp_path, "day", "2026-09-09T10:10:00")
    result = check([("swing", swing), ("day", day)], now=MIDSESSION)
    assert result.failing == ["day"]
    assert result.healthy is False


# ------------------------------------------------------------------- the state


def test_state_survives_a_round_trip_and_a_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / ".watchdog.json"
    WatchdogState(alerting=True, last_alert_at=MIDSESSION).save(path)
    assert WatchdogState.load(path).alerting is True

    path.write_text("{not json")
    assert WatchdogState.load(path).alerting is False  # a fresh start, not a crash

    assert WatchdogState.load(tmp_path / "nope.json").alerting is False
