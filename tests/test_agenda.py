"""Which findings earn a ticket — and, more importantly, which do not.

The whole value of this module is restraint. Four stored sessions produced 43
findings from twelve distinct keys; a promoter that files eagerly turns a daily
message nobody reads into a backlog nobody reads. So most of what is guarded
here is refusal: a hypothesis is never filed, a one-night finding waits, and a
finding that has stopped firing is never filed however persistent it once was.

The third state is the one worth the build. A finding that goes QUIET after a
rule changed is the only evidence in trd that the change did anything, so it has
to survive every other rule here.
"""

from datetime import date

import pytest

from trd.services.agenda import (
    DEFAULT_MIN_SESSIONS,
    AgendaState,
    agenda,
    histories,
)

DAYS = [date(2026, 9, d) for d in (4, 8, 10, 11)]


def finding(
    key: str = "capture.momentum",
    engine: str = "day",
    n: int = 200,
    hypothesis: bool = False,
    **kw,
) -> dict:
    return {
        "key": key,
        "engine": engine,
        "scope": "strategy",
        "subject": "momentum",
        "headline": f"{key} said something",
        "detail": "why it matters",
        "n": n,
        "hypothesis": hypothesis,
        "evidence": {"capture": "7%"},
        "test": "trd engine backtest --years 5",
        **kw,
    }


def sessions(*rows: tuple[date, list[dict]], engine: str = "day"):
    """Stored reviews newest first, as the repo hands them over."""
    payloads = [(on, {"review": {"findings": found}}) for on, found in rows]
    return [(engine, list(reversed(payloads)))]


def built(*rows, window: int = 10, min_sessions: int = DEFAULT_MIN_SESSIONS, engine="day"):
    return agenda(
        sessions(*rows, engine=engine),
        on=date(2026, 9, 12),
        window=window,
        min_sessions=min_sessions,
    )


# ------------------------------------------------------------------ promotion


def test_a_persistent_finding_with_a_real_sample_is_ready() -> None:
    result = built(*[(day, [finding()]) for day in DAYS])
    assert [h.key for h in result.ready] == ["capture.momentum"]
    assert result.ready[0].sessions == 4


def test_the_persistence_bar_is_a_boundary_not_a_suggestion() -> None:
    """Two sessions is a coincidence that happened twice."""
    two = built(*[(day, [finding()]) for day in DAYS[2:]], min_sessions=3)
    assert two.ready == []
    assert [h.key for h in two.watching] == ["capture.momentum"]

    three = built(*[(day, [finding()]) for day in DAYS[1:]], min_sessions=3)
    assert [h.key for h in three.ready] == ["capture.momentum"]


def test_a_hypothesis_is_never_filed_however_persistent() -> None:
    """Under twenty closed trades the review already calls it a hunch. A hunch
    that has been repeated four times is still a hunch — the review recomputes
    from scratch nightly, so those are not four independent samples."""
    result = built(*[(day, [finding(n=9, hypothesis=True)]) for day in DAYS])
    assert result.ready == []
    watching = result.watching[0]
    assert watching.sessions == 4
    assert watching.state(result.min_sessions) is AgendaState.WATCHING


def test_waiting_names_what_is_missing() -> None:
    """ "Not enough trades" and "not enough nights" are different waits with
    different ends, and a reader can act on exactly one of them."""
    from trd.cli.render import _why_waiting

    thin = built(*[(day, [finding(n=9, hypothesis=True)]) for day in DAYS]).watching[0]
    assert "9 closed trades" in _why_waiting(thin, 3)

    new = built((DAYS[-1], [finding()])).watching[0]
    assert "2 more sessions" in _why_waiting(new, 3)


# ---------------------------------------------------------------- going quiet


def test_a_finding_that_stopped_firing_is_quiet_not_ready() -> None:
    """The point of the whole module. A problem that has gone away must never
    produce a ticket, however many sessions it once survived — and its silence
    is the only evidence in trd that a change to the rules worked."""
    result = built(
        (DAYS[0], [finding()]),
        (DAYS[1], [finding()]),
        (DAYS[2], [finding()]),
        (DAYS[3], []),  # stopped
    )
    assert result.ready == []
    gone = result.quiet[0]
    assert gone.key == "capture.momentum"
    assert gone.sessions == 3
    assert gone.last_seen == DAYS[2]
    assert gone.still_firing is False


def test_a_finding_that_reappears_is_ready_again() -> None:
    """Absence is not a tombstone. A problem that comes back is a problem."""
    result = built(
        (DAYS[0], [finding()]),
        (DAYS[1], []),
        (DAYS[2], [finding()]),
        (DAYS[3], [finding()]),
    )
    assert [h.key for h in result.ready] == ["capture.momentum"]
    assert result.ready[0].sessions == 3


# -------------------------------------------------------------------- reading


def test_the_newest_session_supplies_the_numbers() -> None:
    """An older n is not more evidence. It is the same rolling analysis over a
    window that has since moved, and taking the biggest would quietly report a
    sample the engine no longer has."""
    result = built(
        (DAYS[0], [finding(n=305)]),
        (DAYS[1], [finding(n=256)]),
        (DAYS[2], [finding(n=220)]),
        (DAYS[3], [finding(n=198)]),
    )
    assert result.ready[0].n == 198


def test_the_window_bounds_what_is_considered() -> None:
    """Older sessions are not merely deprioritised, they are not read."""
    result = built(*[(day, [finding()]) for day in DAYS], window=2, min_sessions=2)
    assert result.sessions_read == 2
    assert result.ready[0].sessions == 2
    assert result.ready[0].first_seen == DAYS[2]  # the two older sessions are gone


def test_a_window_narrower_than_the_bar_explains_itself() -> None:
    """--window 2 with --min-sessions 3 can never file anything. Silently
    returning an empty agenda would read as "nothing to do"."""
    result = built(*[(day, [finding()]) for day in DAYS], window=2, min_sessions=3)
    assert result.ready == []
    assert any("Only 2 stored reviews" in c for c in result.caveats)


def test_findings_are_filtered_by_their_own_engine() -> None:
    """Snapshots written before SB-1061 carry BOTH engines' findings in every
    payload, and those rows are still the only history there is. Trusting the
    database a row was read from would attribute the swing engine's work to the
    day engine on every one of them."""
    mixed = [
        finding(key="capture.momentum", engine="day"),
        finding(key="capture.pullback", engine="swing"),
    ]
    result = built(*[(day, mixed) for day in DAYS], engine="day")
    assert [h.key for h in result.histories] == ["capture.momentum"]


def test_two_engines_keep_their_findings_apart() -> None:
    """The same key on two engines is two problems about two rule sets."""
    rows = [
        ("day", [(d, {"review": {"findings": [finding(engine="day")]}}) for d in DAYS]),
        ("swing", [(d, {"review": {"findings": [finding(engine="swing")]}}) for d in DAYS]),
    ]
    result = agenda(rows, on=date(2026, 9, 12))
    assert {h.engine for h in result.ready} == {"day", "swing"}
    assert len(result.ready) == 2


# --------------------------------------------------------------------- output


def test_a_ready_finding_carries_a_ticket_a_human_could_act_on() -> None:
    """The body has to stand alone: someone reading the ticket in a month must
    not have to go back to the review that produced it."""
    result = built(*[(day, [finding()]) for day in DAYS])
    ticket = result.tickets[0]
    assert ticket["title"] == "day: capture.momentum said something"
    body = ticket["body"]
    assert "why it matters" in body  # what it claimed
    assert "fired on 4 of the 4 stored review sessions" in body  # how long
    assert "n=200" in body  # the sample
    assert "capture" in body and "7%" in body  # the evidence
    assert "trd engine backtest --years 5" in body  # what would settle it
    assert "QUIET" in body  # how it closes


def test_only_ready_findings_become_tickets() -> None:
    result = built(*[(day, [finding(n=9, hypothesis=True)]) for day in DAYS])
    assert result.tickets == []


def test_too_few_stored_reviews_says_so_rather_than_reporting_nothing() -> None:
    """An empty agenda on day one is correct and reads exactly like a broken
    one. The difference has to be stated."""
    result = built((DAYS[-1], [finding()]), min_sessions=3)
    assert result.ready == []
    assert any("Only 1 stored review" in c for c in result.caveats)


def test_no_snapshots_at_all_is_not_an_error() -> None:
    result = agenda([], on=date(2026, 9, 12))
    assert result.histories == []
    assert result.sessions_read == 0


@pytest.mark.parametrize("shape", [{}, {"review": {}}, {"review": {"findings": []}}])
def test_a_payload_without_findings_is_skipped_not_fatal(shape: dict) -> None:
    """One malformed stored review must not cost the agenda every other session."""
    rows = [("day", [(DAYS[0], shape), (DAYS[1], {"review": {"findings": [finding()]}})])]
    assert len(histories(rows)) == 1


# ------------------------------------------------------------------------ CLI


def test_cli_reads_the_stored_reviews_and_emits_tickets(tmp_path) -> None:
    """End to end over a real database, including the read-only connection: the
    agenda must never be able to take the writer lock a scan needs."""
    import json

    from typer.testing import CliRunner

    from trd.cli.app import app
    from trd.db.connection import connect
    from trd.repos.review_snapshot import ReviewSnapshotRepo, ReviewSnapshotRow

    home = tmp_path / "day"
    home.mkdir()
    conn = connect(home / "trd.duckdb")
    repo = ReviewSnapshotRepo(conn)
    for on in DAYS:
        repo.save(
            ReviewSnapshotRow(
                snapshot_date=on,
                generated_at=__import__("datetime").datetime(2026, 9, 11, 16, 31),
                build="test",
                engine="day",
                findings=1,
                quiet=False,
            ),
            payload={"review": {"findings": [finding()]}},
        )
    conn.close()

    result = CliRunner().invoke(app, ["engine", "agenda", "--engines", f"day={home}", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sessions_read"] == 4
    assert [t["key"] for t in payload["tickets"]] == ["capture.momentum"]
    assert payload["tickets"][0]["body"]


def test_cli_survives_an_engine_whose_database_is_missing(tmp_path) -> None:
    """A share that did not mount must not cost the other engine its agenda."""
    import json

    from typer.testing import CliRunner

    from trd.cli.app import app

    result = CliRunner().invoke(
        app, ["engine", "agenda", "--engines", f"day={tmp_path / 'gone'}", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["histories"] == []
    assert any("no database" in c for c in payload["caveats"])
