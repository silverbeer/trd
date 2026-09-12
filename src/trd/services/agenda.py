"""Which findings have earned a ticket, and which have quietly gone away.

The review recomputes from scratch every night over a trailing window. It does
not remember yesterday, so a problem that has been true for a month is reported
as a fresh discovery every single night — which is exactly how a daily message
stops being read, and why nothing has ever been done about any of it.

This is the memory. It reads the stored reviews, groups every finding by its
`key`, and asks one question: has this survived?

**The unit of a ticket is the key, not the night.** Measured on the first four
stored sessions: 43 findings emitted, twelve distinct keys behind them. Filing
per finding per night would produce roughly three hundred tickets a month
describing twelve problems, which is not a backlog but a way of hiding one.

Three states, and the third is the reason to build this at all:

- **READY** — enough trades behind it, fired on enough of the recent sessions,
  and still firing. This is work.
- **WATCHING** — real but unproven: too few trades, or not yet persistent.
  Named so it can be seen coming, never filed. A hypothesis that reads like a
  finding is how a backlog fills with work nobody should do.
- **QUIET** — it used to fire and has stopped. That silence is the only
  evidence that a change actually worked, and nothing else in trd can see it.

Nothing here writes, and nothing here knows what Linear is. The command emits
an agenda; an agent files it. Same rule that keeps the broker's MCP session out
of `src/trd`: the thing that trades money does not reach out to other systems.
"""

from collections import defaultdict
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, computed_field

# How many stored sessions are considered. Ten review nights is about a
# fortnight of trading — long enough that a finding has had to survive a
# changing window, short enough that a problem fixed last month is not still
# being argued about.
DEFAULT_WINDOW_SESSIONS = 10

# Sessions a finding must appear in before it is worth filing. Below this it
# may still be a one-night artefact of which trades happened to fall inside the
# trailing window.
DEFAULT_MIN_SESSIONS = 3


class AgendaState(StrEnum):
    READY = "READY"  # proven and still firing: file it
    WATCHING = "WATCHING"  # firing, not yet proven
    QUIET = "QUIET"  # stopped firing — the evidence a change worked


class FindingHistory(BaseModel):
    """One finding key across every session it appeared in.

    Identity is (engine, key). `key` is built by the detector as
    `<detector>.<subject>` — `capture.momentum`, `stop-room.pullback` — and is
    deterministic, so recognising a repeat is a string comparison rather than a
    judgement about whether two sentences mean the same thing.
    """

    engine: str
    key: str
    scope: str
    subject: str
    # The most recent wording. Headlines carry their own numbers, so they drift
    # night to night while the key does not — which is the point of the key.
    headline: str
    detail: str = ""
    seen_on: list[date] = []
    # The newest session's numbers. An older n is not more evidence, it is the
    # same rolling analysis over a window that has since moved.
    n: int = 0
    hypothesis: bool = True
    evidence: dict[str, str] = {}
    test: str | None = None
    # The newest session in the window, so "is it still firing" is answerable
    # without carrying the whole snapshot list around.
    latest_session: date | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sessions(self) -> int:
        return len(self.seen_on)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def first_seen(self) -> date | None:
        return min(self.seen_on) if self.seen_on else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def last_seen(self) -> date | None:
        return max(self.seen_on) if self.seen_on else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def still_firing(self) -> bool:
        """Present in the most recent session reviewed.

        A finding that was persistent and has stopped must never be READY,
        however many sessions it once survived: filing a ticket for a problem
        that has already gone away is worse than filing nothing.
        """
        return self.last_seen is not None and self.last_seen == self.latest_session

    def state(self, min_sessions: int) -> AgendaState:
        if not self.still_firing:
            return AgendaState.QUIET
        if self.hypothesis or self.sessions < min_sessions:
            return AgendaState.WATCHING
        return AgendaState.READY

    @computed_field  # type: ignore[prop-decorator]
    @property
    def title(self) -> str:
        """The ticket's one line. Engine-scoped, because the same key on the
        swing and day engines is two different problems about two rule sets."""
        return f"{self.engine}: {self.headline}"

    def body(self, read: int, min_sessions: int) -> str:
        """The ticket, ready to file.

        Carries what the finding claimed, how long it has claimed it, the
        evidence, and the test that would settle it — so the ticket can be
        judged without going back to the review that produced it.
        """
        lines = [
            self.detail,
            "",
            f"**Finding key:** `{self.key}` · scope {self.scope} · subject {self.subject}",
            f"**Engine:** {self.engine}",
            f"**Persistence:** fired on {self.sessions} of the {read} stored review "
            f"session{'' if read == 1 else 's'} ({self.first_seen} → {self.last_seen}), "
            "still firing.",
            f"**Sample:** n={self.n} trades behind the most recent occurrence.",
        ]
        if self.evidence:
            lines += ["", "**Evidence (most recent session):**"]
            lines += [f"- `{k}` {v}" for k, v in sorted(self.evidence.items())]
        if self.test:
            lines += ["", f"**What would settle it:** `{self.test}`"]
        lines += [
            "",
            "---",
            f"Filed from `trd engine agenda` because this finding cleared both bars: at "
            f"least {min_sessions} sessions of firing, and enough closed trades to stop "
            "being a hypothesis. The review recomputes from scratch nightly, so this is "
            "the same conclusion reached independently on each of those sessions.",
            "",
            "It closes when the finding stops firing — `trd engine agenda` reports that as "
            "QUIET, which is the only evidence that a change to the rules actually worked.",
        ]
        return "\n".join(lines)


class Agenda(BaseModel):
    """Every finding key in the window, sorted into what to do about it."""

    on: date
    window: int = DEFAULT_WINDOW_SESSIONS
    min_sessions: int = DEFAULT_MIN_SESSIONS
    sessions_read: int = 0
    engines: list[str] = []
    histories: list[FindingHistory] = []
    caveats: list[str] = []

    def _in(self, state: AgendaState) -> list[FindingHistory]:
        return [h for h in self.histories if h.state(self.min_sessions) is state]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ready(self) -> list[FindingHistory]:
        return self._in(AgendaState.READY)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def watching(self) -> list[FindingHistory]:
        return self._in(AgendaState.WATCHING)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def quiet(self) -> list[FindingHistory]:
        return self._in(AgendaState.QUIET)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def tickets(self) -> list[dict]:
        """The READY findings as titles and bodies, for whatever files them.

        Rendered here rather than by the caller so the terminal, the JSON and an
        agent all file the same words — a ticket assembled twice is two tickets
        that slowly stop matching.
        """
        return [
            {
                "key": h.key,
                "engine": h.engine,
                "title": h.title,
                "body": h.body(self.sessions_read, self.min_sessions),
            }
            for h in self.ready
        ]


def histories(
    snapshots: list[tuple[str, list[tuple[date, dict]]]],
    window: int = DEFAULT_WINDOW_SESSIONS,
) -> list[FindingHistory]:
    """Group every stored finding by (engine, key) over the last `window` sessions.

    `snapshots` is one entry per engine — its name and its stored reviews, newest
    first, as `ReviewSnapshotRepo.recent_payloads` returns them.

    Findings are filtered by their own `engine` field rather than trusted to the
    database they were read from. Snapshots written before SB-1061 carry both
    engines' findings in every payload, and those rows are still the only history
    there is — so the filter is what makes them readable rather than discarded.
    """
    grouped: dict[tuple[str, str], FindingHistory] = {}
    order: list[tuple[str, str]] = []
    for engine, rows in snapshots:
        # Sorted here rather than trusted to the caller. "The last N sessions"
        # and "newest first" are both load-bearing — the window decides what is
        # considered at all, and the order decides which session's numbers are
        # taken as current.
        recent = sorted(rows, key=lambda r: r[0], reverse=True)[:window]
        if not recent:
            continue
        latest = max(on for on, _ in recent)
        for on, payload in recent:
            for raw in _findings(payload, engine):
                ident = (engine, raw["key"])
                history = grouped.get(ident)
                if history is None:
                    # Rows arrive newest first, so the first sighting of a key is
                    # its freshest. Every field carrying a number is taken here
                    # and never overwritten: an older n is not more evidence, it
                    # is the same rolling analysis over a window that has moved.
                    history = FindingHistory(
                        engine=engine,
                        key=raw["key"],
                        scope=raw.get("scope", ""),
                        subject=raw.get("subject", ""),
                        headline=raw.get("headline", ""),
                        detail=raw.get("detail", ""),
                        n=int(raw.get("n", 0)),
                        hypothesis=bool(raw.get("hypothesis", True)),
                        evidence=raw.get("evidence") or {},
                        test=raw.get("test"),
                        latest_session=latest,
                        seen_on=[],
                    )
                    grouped[ident] = history
                    order.append(ident)
                if on not in history.seen_on:
                    history.seen_on.append(on)
    return [grouped[i] for i in order]


def _findings(payload: dict, engine: str) -> list[dict]:
    """This engine's findings out of one stored review, whatever shape it is in."""
    review = payload.get("review") or {}
    return [f for f in review.get("findings", []) if f.get("engine") == engine]


def agenda(
    snapshots: list[tuple[str, list[tuple[date, dict]]]],
    on: date,
    window: int = DEFAULT_WINDOW_SESSIONS,
    min_sessions: int = DEFAULT_MIN_SESSIONS,
    caveats: list[str] | None = None,
) -> Agenda:
    """The whole agenda: what to file, what to watch, what has gone away.

    Sorted so the strongest claim is first — most sessions survived, then the
    largest sample. A list whose top line is the flimsiest thing on it trains
    its reader to skim.
    """
    found = histories(snapshots, window)
    found.sort(key=lambda h: (h.sessions, h.n), reverse=True)
    read = max((len(rows[:window]) for _, rows in snapshots), default=0)
    notes = list(caveats or [])
    if read and read < min_sessions:
        notes.append(
            f"Only {read} stored review{'' if read == 1 else 's'} to read — nothing can "
            f"clear {min_sessions} sessions yet. Run the nightly review for a few days."
        )
    return Agenda(
        on=on,
        window=window,
        min_sessions=min_sessions,
        sessions_read=read,
        engines=[name for name, _ in snapshots],
        histories=found,
        caveats=notes,
    )


def by_engine(histories_: list[FindingHistory]) -> dict[str, list[FindingHistory]]:
    """Group for rendering, preserving the ranking within each engine."""
    out: dict[str, list[FindingHistory]] = defaultdict(list)
    for history in histories_:
        out[history.engine].append(history)
    return dict(out)
