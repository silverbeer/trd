"""The recurring macro calendar — the events that move the whole market.

Three sources, in order of reliability:

1. FOMC decisions — exact, published by the Fed a year ahead. Decision + press
   conference land on the second day at 2:00 PM ET.
2. Computed rules — Nonfarm Payrolls (first Friday, 8:30 AM ET) and weekly Initial
   Jobless Claims (every Thursday, 8:30 AM ET). These are derived, never stale.
3. SCHEDULED_RELEASES — CPI/PPI/Retail Sales/GDP/PCE/Sentiment. Their dates shift
   month to month (BLS/BEA set them), so this is a *maintained* table of best-effort
   dates. Verify against bls.gov / bea.gov and extend as the year rolls forward; an
   econ-calendar API can replace it wholesale later.

All times are US Eastern. `events_for_week` merges all three for a date window.
"""

from datetime import date, timedelta

from pydantic import BaseModel


class EconEvent(BaseModel):
    day: str  # weekday name, e.g. "Wednesday"
    date: date
    time_et: str  # e.g. "8:30 AM ET", or "" when intraday timing varies
    name: str
    why: str


# FOMC interest-rate decision days (second day of each 2026 meeting), 2:00 PM ET.
FOMC_DECISIONS: list[date] = [
    date(2026, 1, 28),
    date(2026, 3, 18),
    date(2026, 4, 29),
    date(2026, 6, 17),
    date(2026, 7, 29),
    date(2026, 9, 16),
    date(2026, 10, 28),
    date(2026, 12, 9),
]

# Maintained best-effort table — VERIFY against the source agencies and extend.
# (name, date, time_et, why)
SCHEDULED_RELEASES: list[tuple[str, date, str, str]] = [
    (
        "CPI (Consumer Price Index)",
        date(2026, 6, 10),
        "8:30 AM ET",
        "The headline inflation read; the single biggest input to rate expectations.",
    ),
    (
        "PPI (Producer Price Index)",
        date(2026, 6, 11),
        "8:30 AM ET",
        "Wholesale inflation — an early tell on where CPI is heading.",
    ),
    (
        "Retail Sales",
        date(2026, 6, 17),
        "8:30 AM ET",
        "The pulse of the consumer, ~70% of the economy. Strength supports the soft-landing case.",
    ),
    (
        "Housing Starts",
        date(2026, 6, 18),
        "8:30 AM ET",
        "Rate-sensitive housing activity — a read on how restrictive policy feels.",
    ),
    (
        "Univ. of Michigan Consumer Sentiment",
        date(2026, 6, 26),
        "10:00 AM ET",
        "Confidence and, critically, inflation expectations the Fed watches closely.",
    ),
    (
        "PCE Price Index",
        date(2026, 6, 26),
        "8:30 AM ET",
        "The Fed's preferred inflation gauge — what the dot plot is actually calibrated to.",
    ),
    (
        "GDP (2nd estimate)",
        date(2026, 6, 25),
        "8:30 AM ET",
        "The scorecard on overall growth; revisions can shift the recession debate.",
    ),
]


def _weekday_name(d: date) -> str:
    return ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[
        d.weekday()
    ]


def _fomc_events(start: date, end: date) -> list[EconEvent]:
    out: list[EconEvent] = []
    for d in FOMC_DECISIONS:
        if start <= d <= end:
            out.append(
                EconEvent(
                    day=_weekday_name(d),
                    date=d,
                    time_et="2:00 PM ET",
                    name="FOMC Rate Decision + Press Conference",
                    why=(
                        "The week's main event. The rate call, the statement wording, and the "
                        "dot plot reset rate expectations across every asset class."
                    ),
                )
            )
    return out


def _nonfarm_payrolls(start: date, end: date) -> list[EconEvent]:
    """First Friday of any month touched by the window, 8:30 AM ET."""
    out: list[EconEvent] = []
    seen: set[date] = set()
    for d in (start, end):
        first = d.replace(day=1)
        # advance to the first Friday (weekday 4)
        offset = (4 - first.weekday()) % 7
        friday = first + timedelta(days=offset)
        if start <= friday <= end and friday not in seen:
            seen.add(friday)
            out.append(
                EconEvent(
                    day="Friday",
                    date=friday,
                    time_et="8:30 AM ET",
                    name="Nonfarm Payrolls (Jobs Report)",
                    why=(
                        "The most important labor read. Hot jobs = fewer cuts priced; weak jobs = "
                        "growth-scare. Moves rates, the dollar, and equities together."
                    ),
                )
            )
    return out


def _jobless_claims(start: date, end: date) -> list[EconEvent]:
    """Every Thursday in the window, 8:30 AM ET."""
    out: list[EconEvent] = []
    d = start
    while d <= end:
        if d.weekday() == 3:  # Thursday
            out.append(
                EconEvent(
                    day="Thursday",
                    date=d,
                    time_et="8:30 AM ET",
                    name="Initial Jobless Claims",
                    why=(
                        "Weekly, high-frequency labor read. A rising trend is the earliest crack "
                        "in the soft-landing story."
                    ),
                )
            )
        d += timedelta(days=1)
    return out


def _scheduled(start: date, end: date) -> list[EconEvent]:
    out: list[EconEvent] = []
    for name, d, time_et, why in SCHEDULED_RELEASES:
        if start <= d <= end:
            out.append(EconEvent(day=_weekday_name(d), date=d, time_et=time_et, name=name, why=why))
    return out


def events_for_week(start: date, end: date) -> list[EconEvent]:
    """All known macro events in [start, end], inclusive, sorted by date then time."""
    events = (
        _fomc_events(start, end)
        + _scheduled(start, end)
        + _nonfarm_payrolls(start, end)
        + _jobless_claims(start, end)
    )
    return sorted(events, key=lambda e: (e.date, e.time_et))
