"""The recurring macro calendar — the events that move the whole market.

Three sources, in order of reliability:

1. FOMC decisions — exact, published by the Fed a year ahead. Decision + press
   conference land on the second day at 2:00 PM ET.
2. Computed rules — Nonfarm Payrolls (first Friday, 8:30 AM ET) and weekly Initial
   Jobless Claims (every Thursday, 8:30 AM ET). The RULE is derived and never stale;
   the DATE it lands on still depends on the holiday table below. A Thursday that is a
   federal holiday has no release: the Department of Labor publishes a day early
   (SB-1755 — the briefing for Thanksgiving week 2026 dated claims on the holiday).
3. SCHEDULED_RELEASES — CPI/PPI/Retail Sales/GDP/PCE/Sentiment. Their dates shift
   month to month (BLS/BEA set them), so this is a *maintained* table of best-effort
   dates. Verify against bls.gov / bea.gov and extend as the year rolls forward; an
   econ-calendar API can replace it wholesale later.

4. MARKET_HOLIDAYS / EARLY_CLOSES — the exchange's own published calendar, a
   *maintained* table like SCHEDULED_RELEASES. It ends at CALENDAR_THROUGH; a week past
   it is reported as unchecked (`calendar_covers`), never as a week with no holiday.

All times are US Eastern. `events_for_week` merges the first three for a date window;
`closures_for_week` answers the fourth, and `session_close` is the bell the engine
works to on a given day (SB-1775).
"""

from datetime import date, time, timedelta

from pydantic import BaseModel


class EconEvent(BaseModel):
    day: str  # weekday name, e.g. "Wednesday"
    date: date
    time_et: str  # e.g. "8:30 AM ET", or "" when intraday timing varies
    name: str
    why: str


class MarketClosure(BaseModel):
    """A session the US stock market skips or cuts short."""

    day: str  # weekday name
    date: date
    name: str  # the holiday, e.g. "Thanksgiving Day"
    kind: str  # "closed" | "early_close"
    close_time_et: str  # "1:00 PM ET" on an early close, "" on a full closure


# US stock-market full closures, from the exchange's published 2026-2028 holiday
# calendar (looked up 2026-11-22). Observed dates are the ones listed: a holiday on a
# Saturday is observed the Friday before, on a Sunday the Monday after — and New
# Year's Day 2028 (a Saturday) is not observed at all.
MARKET_HOLIDAYS: dict[date, str] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King Jr. Day",
    date(2026, 2, 16): "Presidents' Day",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King Jr. Day",
    date(2027, 2, 15): "Presidents' Day",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
    date(2028, 1, 17): "Martin Luther King Jr. Day",
    date(2028, 2, 21): "Presidents' Day",
    date(2028, 4, 14): "Good Friday",
    date(2028, 5, 29): "Memorial Day",
    date(2028, 6, 19): "Juneteenth",
    date(2028, 7, 4): "Independence Day",
    date(2028, 9, 4): "Labor Day",
    date(2028, 11, 23): "Thanksgiving Day",
    date(2028, 12, 25): "Christmas Day",
}

# Sessions that end at 1:00 PM ET, same source.
EARLY_CLOSE_TIME = "1:00 PM ET"
EARLY_CLOSES: dict[date, str] = {
    date(2026, 11, 27): "day after Thanksgiving",
    date(2026, 12, 24): "Christmas Eve",
    date(2027, 11, 26): "day after Thanksgiving",
    date(2028, 7, 3): "day before Independence Day",
    date(2028, 11, 24): "day after Thanksgiving",
}

# The span the two tables above were read for. Outside it nothing is known.
CALENDAR_FROM = date(2026, 1, 1)
CALENDAR_THROUGH = date(2028, 12, 31)

# The bell, ET. A regular session ends at 16:00; one in EARLY_CLOSES at 13:00.
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# The one market holiday that is NOT a federal holiday: agencies publish as usual.
_NOT_FEDERAL = "Good Friday"


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


def calendar_covers(start: date, end: date) -> bool:
    """Whether the holiday tables were read for every day of [start, end]."""
    return start >= CALENDAR_FROM and end <= CALENDAR_THROUGH


def is_federal_holiday(d: date) -> bool:
    """A day federal agencies do not publish on, as far as the tables know.

    Market holidays minus Good Friday, plus Veterans Day (11 November), which
    closes the agencies and not the market. Columbus Day does too, but it is
    always a Monday and no rule here lands on a Monday.
    """
    if d.month == 11 and d.day == 11:
        return True
    name = MARKET_HOLIDAYS.get(d)
    return name is not None and name != _NOT_FEDERAL


def session_close(d: date) -> time:
    """The instant the session on `d` ends, exchange-local: 13:00 on an early
    close, 16:00 otherwise.

    Only the dates in EARLY_CLOSES are known. A half-day outside CALENDAR_FROM ..
    CALENDAR_THROUGH — a backtest over 2025's day after Thanksgiving, say — reads
    as a full session, because nothing here says otherwise. A holiday reads as
    16:00 too: there is no session to end, and nothing scans on one.
    """
    return EARLY_CLOSE if d in EARLY_CLOSES else REGULAR_CLOSE


def session_close_minute(d: date) -> int:
    """`session_close(d)` as minute-of-day: 780 on an early close, 960 otherwise."""
    close = session_close(d)
    return close.hour * 60 + close.minute


def closures_for_week(start: date, end: date) -> list[MarketClosure]:
    """Full closures and early closes in [start, end], inclusive, by date."""
    out = [
        MarketClosure(day=_weekday_name(d), date=d, name=name, kind="closed", close_time_et="")
        for d, name in MARKET_HOLIDAYS.items()
        if start <= d <= end
    ]
    out += [
        MarketClosure(
            day=_weekday_name(d),
            date=d,
            name=name,
            kind="early_close",
            close_time_et=EARLY_CLOSE_TIME,
        )
        for d, name in EARLY_CLOSES.items()
        if start <= d <= end
    ]
    return sorted(out, key=lambda c: c.date)


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
    """First Friday of any month touched by the window, 8:30 AM ET.

    A first Friday that is a federal holiday (3 July 2026, 1 January 2027) has no
    release, and where it moves to is the agency's call, not a rule — so nothing
    is emitted rather than a date that is known to be wrong.
    """
    out: list[EconEvent] = []
    seen: set[date] = set()
    for d in (start, end):
        first = d.replace(day=1)
        # advance to the first Friday (weekday 4)
        offset = (4 - first.weekday()) % 7
        friday = first + timedelta(days=offset)
        if start <= friday <= end and friday not in seen and not is_federal_holiday(friday):
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
    """Every Thursday in the window, 8:30 AM ET — a day early when that Thursday
    is a federal holiday.

    The Department of Labor's schedule lists Wednesday 25 November 2026 for
    Thanksgiving week (looked up 2026-11-22); the day-early move is its standing
    practice for a holiday Thursday.
    """
    out: list[EconEvent] = []
    d = start
    while d <= end:
        if d.weekday() == 3:  # Thursday
            moved = is_federal_holiday(d)
            release = d - timedelta(days=1) if moved else d
            why = (
                "Weekly, high-frequency labor read. A rising trend is the earliest crack "
                "in the soft-landing story."
            )
            if moved:
                why += " Out a day early: Thursday is a federal holiday."
            if start <= release <= end:
                out.append(
                    EconEvent(
                        day=_weekday_name(release),
                        date=release,
                        time_et="8:30 AM ET",
                        name="Initial Jobless Claims",
                        why=why,
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
