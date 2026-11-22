"""The macro calendar's holiday awareness (SB-1755).

The briefing for Thanksgiving week 2026 dated Initial Jobless Claims on the
holiday itself and said nothing about the week being short.
"""

from datetime import date

from trd.data.market_calendar import (
    CALENDAR_THROUGH,
    EARLY_CLOSES,
    MARKET_HOLIDAYS,
    calendar_covers,
    closures_for_week,
    events_for_week,
    is_federal_holiday,
)

THANKSGIVING_WEEK = (date(2026, 11, 23), date(2026, 11, 27))
ORDINARY_WEEK = (date(2026, 11, 16), date(2026, 11, 20))


def _claims(start: date, end: date):
    return [e for e in events_for_week(start, end) if e.name == "Initial Jobless Claims"]


def test_claims_land_on_thursday_in_an_ordinary_week() -> None:
    (event,) = _claims(*ORDINARY_WEEK)
    assert event.date == date(2026, 11, 19)
    assert event.day == "Thursday"
    assert "early" not in event.why


def test_claims_move_a_day_early_when_thursday_is_a_holiday() -> None:
    (event,) = _claims(*THANKSGIVING_WEEK)
    assert event.date == date(2026, 11, 25)
    assert event.day == "Wednesday"
    assert event.time_et == "8:30 AM ET"
    assert "federal holiday" in event.why


def test_no_event_is_dated_on_a_federal_holiday() -> None:
    """Every derived rule, every week the tables cover."""
    events = events_for_week(date(2026, 1, 1), CALENDAR_THROUGH)
    assert events
    assert [e for e in events if is_federal_holiday(e.date)] == []


def test_veterans_day_closes_the_agencies_but_not_the_market() -> None:
    # 11 November 2027 is a Thursday: claims move, the market trades.
    (event,) = _claims(date(2027, 11, 8), date(2027, 11, 12))
    assert event.date == date(2027, 11, 10)
    assert closures_for_week(date(2027, 11, 8), date(2027, 11, 12)) == []


def test_good_friday_closes_the_market_but_not_the_agencies() -> None:
    # 3 April 2026: first Friday of the month AND Good Friday. Payrolls still print.
    names = [e.name for e in events_for_week(date(2026, 3, 30), date(2026, 4, 3))]
    assert any("Payrolls" in n for n in names)
    assert not is_federal_holiday(date(2026, 4, 3))


def test_payrolls_are_not_dated_on_a_federal_holiday() -> None:
    # 3 July 2026 is the first Friday and the observed Independence Day.
    names = [e.name for e in events_for_week(date(2026, 6, 29), date(2026, 7, 3))]
    assert not any("Payrolls" in n for n in names)


def test_closures_name_the_holiday_and_the_early_close() -> None:
    closed, early = closures_for_week(*THANKSGIVING_WEEK)
    assert (closed.date, closed.kind, closed.day) == (date(2026, 11, 26), "closed", "Thursday")
    assert closed.name == "Thanksgiving Day" and closed.close_time_et == ""
    assert (early.date, early.kind, early.day) == (date(2026, 11, 27), "early_close", "Friday")
    assert early.close_time_et == "1:00 PM ET"


def test_an_ordinary_week_has_no_closure() -> None:
    assert closures_for_week(*ORDINARY_WEEK) == []


def test_the_tables_are_weekdays_and_do_not_overlap() -> None:
    assert all(d.weekday() < 5 for d in [*MARKET_HOLIDAYS, *EARLY_CLOSES])
    assert not set(MARKET_HOLIDAYS) & set(EARLY_CLOSES)
    # Every Thanksgiving is the fourth Thursday of November.
    for d, name in MARKET_HOLIDAYS.items():
        if name == "Thanksgiving Day":
            assert d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28


def test_a_week_past_the_table_is_unchecked_not_holiday_free() -> None:
    assert calendar_covers(*THANKSGIVING_WEEK)
    assert not calendar_covers(date(2029, 1, 1), date(2029, 1, 5))
    assert not calendar_covers(date(2028, 12, 25), date(2029, 1, 1))


def test_the_holiday_table_has_not_run_out() -> None:
    """Fails 90 days before the table ends, so it is extended before a briefing
    has to say 'not checked'. Extend MARKET_HOLIDAYS / EARLY_CLOSES from the
    exchange's published calendar and move CALENDAR_THROUGH."""
    assert (CALENDAR_THROUGH - date.today()).days > 90
