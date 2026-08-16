"""The point-in-time earnings archive, and the reschedule reconcile beside it.

Two properties are load-bearing and everything here aims at them.

**Immutability.** A stored estimate is what the tape knew before the number
landed. Nothing may revise it — not a later sync, not the arrival of the actual.
A test suite that only checks "the row exists" would pass against an archive that
quietly rewrites itself into today's values, which is the exact failure the table
was built to prevent.

**Honest absence.** yfinance supplies no revenue, guidance, revisions or BMO/AMC
timing. Those must read as unknown rather than as zero, so `quality_status` is
asserted alongside the values.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

import duckdb
import pytest

from trd.models import DailyBar, EarningsDate, InstrumentInfo, Side
from trd.repos import EarningsRepo, EarningsResultRepo, InstrumentRepo, PriceRepo
from trd.services import EarningsArchiveService, PortfolioService, SyncService

TODAY = date(2026, 8, 14)
NOW = datetime(2026, 8, 14, 9, 30)


def _instrument(conn: duckdb.DuckDBPyConnection, symbol: str = "AAA"):
    repo = InstrumentRepo(conn)
    return repo.get_by_symbol(symbol) or repo.insert(InstrumentInfo(symbol=symbol, name=symbol))


def _seed_bars(conn: duckdb.DuckDBPyConnection, instrument_id: int, closes: list[float]) -> None:
    """Daily bars ending on TODAY, one per calendar day."""
    first = TODAY - timedelta(days=len(closes) - 1)
    PriceRepo(conn).upsert_daily(
        instrument_id,
        [
            DailyBar(
                date=first + timedelta(days=i),
                open=Decimal(str(close)),
                high=Decimal(str(close)),
                low=Decimal(str(close)),
                close=Decimal(str(close)),
                volume=1_000,
            )
            for i, close in enumerate(closes)
        ],
    )


# ------------------------------------------------- reschedule reconcile (SB-611)


def test_a_rescheduled_report_does_not_leave_a_second_blackout(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The bug: the primary key is (instrument_id, date), so a moved date *added*
    a row instead of replacing one. Both dates then drove the entry blackout, so
    the engine sat out two windows and nothing said why."""
    instrument = _instrument(conn)
    repo = EarningsRepo(conn)
    original = TODAY + timedelta(days=6)
    moved = TODAY + timedelta(days=13)

    repo.upsert(instrument.id, [EarningsDate(date=original)], today=TODAY)
    assert repo.dates_for_instrument(instrument.id) == [original]

    repo.upsert(instrument.id, [EarningsDate(date=moved)], today=TODAY)
    assert repo.dates_for_instrument(instrument.id) == [moved], "the old date survived"


def test_past_events_survive_a_reconcile(conn: duckdb.DuckDBPyConnection) -> None:
    """A reported quarter is a fact. The provider serving a 12-quarter window and
    eventually dropping it off the end is a horizon, not a cancellation."""
    instrument = _instrument(conn)
    repo = EarningsRepo(conn)
    reported = TODAY - timedelta(days=90)
    repo.upsert(
        instrument.id,
        [EarningsDate(date=reported, eps_actual=Decimal("1.50"))],
        today=TODAY,
    )

    repo.upsert(instrument.id, [EarningsDate(date=TODAY + timedelta(days=5))], today=TODAY)
    assert reported in repo.dates_for_instrument(instrument.id)


def test_an_empty_provider_response_reconciles_nothing(conn: duckdb.DuckDBPyConnection) -> None:
    """'No response' and 'no earnings scheduled' are indistinguishable here, and
    deleting on the former would silently remove the blackout's protection."""
    instrument = _instrument(conn)
    repo = EarningsRepo(conn)
    upcoming = TODAY + timedelta(days=4)
    repo.upsert(instrument.id, [EarningsDate(date=upcoming)], today=TODAY)

    repo.upsert(instrument.id, [], today=TODAY)
    assert repo.dates_for_instrument(instrument.id) == [upcoming]


# -------------------------------------------------------- the archive (SB-615)


def test_the_estimate_is_captured_before_the_number_lands(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The whole reason the archive exists.

    A release is recorded when it is first *announced*, carrying the estimate as
    it then stood. Capturing after the report would archive the post-release
    estimate — a memory of a consensus, not the consensus itself.
    """
    instrument = _instrument(conn)
    archive = EarningsArchiveService(conn)
    upcoming = TODAY + timedelta(days=7)

    archive.capture(
        instrument, [EarningsDate(date=upcoming, eps_estimate=Decimal("2.00"))], now=NOW
    )

    stored = EarningsResultRepo(conn).get(instrument.id, upcoming)
    assert stored is not None
    assert stored.eps_estimate_pre_release == Decimal("2.00")
    assert stored.eps_actual is None
    assert stored.source_observed_at == NOW
    assert stored.observed == ["estimate"]


def test_a_later_sync_never_revises_a_stored_estimate(conn: duckdb.DuckDBPyConnection) -> None:
    """The immutability guarantee, tested the only way that means anything: hand
    the archive a *different* estimate for the same release and prove the first
    one survived."""
    instrument = _instrument(conn)
    archive = EarningsArchiveService(conn)
    released = TODAY - timedelta(days=2)

    archive.capture(
        instrument, [EarningsDate(date=released, eps_estimate=Decimal("2.00"))], now=NOW
    )
    archive.capture(
        instrument,
        [EarningsDate(date=released, eps_estimate=Decimal("2.75"), eps_actual=Decimal("3.10"))],
        now=NOW + timedelta(days=1),
    )

    stored = EarningsResultRepo(conn).get(instrument.id, released)
    assert stored is not None
    assert stored.eps_estimate_pre_release == Decimal("2.00"), "the revision overwrote history"
    # The actual could not have existed at first sighting, so it *is* allowed in.
    assert stored.eps_actual == Decimal("3.10")
    assert stored.observed == ["eps", "estimate"]
    assert stored.source_observed_at == NOW, "the observation time moved with the revision"


def test_unobservable_fields_stay_unknown_rather_than_zero(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """yfinance has no revenue, guidance or revisions. A consumer must be able to
    tell 'not measured' from 'measured as nothing' — the feature request calls
    out silently-favourable defaults by name."""
    instrument = _instrument(conn)
    archive = EarningsArchiveService(conn)
    released = TODAY - timedelta(days=3)
    archive.capture(
        instrument,
        [EarningsDate(date=released, eps_estimate=Decimal("1.00"), eps_actual=Decimal("1.20"))],
        now=NOW,
    )

    stored = EarningsResultRepo(conn).get(instrument.id, released)
    assert stored is not None
    assert stored.revenue_actual is None
    assert stored.revenue_estimate_pre_release is None
    assert stored.next_quarter_revision_pct is None
    assert stored.guidance_direction == "unknown"
    # And release timing, which is what forces a consumer onto the conservative
    # next-session path instead of a BMO fast path it cannot justify.
    assert stored.release_timing == "unknown"
    assert "revenue" not in stored.observed
    assert "guidance" not in stored.observed


def test_eps_surprise_is_undefined_against_a_zero_estimate() -> None:
    """A percentage against a zero base is not a huge surprise, it is undefined —
    and a huge number here would rank a name on an artefact."""
    from trd.models import EarningsResult

    zero = EarningsResult(
        instrument_id=1,
        released_on=TODAY,
        source="yfinance",
        source_observed_at=NOW,
        eps_actual=Decimal("0.40"),
        eps_estimate_pre_release=Decimal("0"),
    )
    assert zero.eps_surprise_pct is None

    beat = zero.model_copy(update={"eps_estimate_pre_release": Decimal("0.50")})
    assert beat.eps_surprise_pct == pytest.approx(-20.0)


def test_the_reaction_is_frozen_once_the_session_settles(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The move, and the move against the tape. Recorded once the daily bar
    exists, then left alone."""
    instrument = _instrument(conn)
    spy = _instrument(conn, "SPY")
    released = TODAY - timedelta(days=1)
    # The name jumps 10% on the report; the tape does 2%. Relative strength +8.
    _seed_bars(conn, instrument.id, [100.0, 100.0, 110.0, 110.0])
    _seed_bars(conn, spy.id, [400.0, 400.0, 408.0, 408.0])

    archive = EarningsArchiveService(conn)
    archive.capture(instrument, [EarningsDate(date=released, eps_actual=Decimal("1.00"))], now=NOW)

    stored = EarningsResultRepo(conn).get(instrument.id, released)
    assert stored is not None
    assert stored.earnings_day_return_pct == pytest.approx(10.0)
    assert stored.earnings_day_relative_strength_pct == pytest.approx(8.0)
    assert "reaction" in stored.observed
    assert "relative_strength" in stored.observed


def test_a_reaction_is_not_invented_without_bars(conn: duckdb.DuckDBPyConnection) -> None:
    """No stored session, no number. The archive's job is to record what was
    observed, and an unobserved reaction is not a reaction of zero."""
    instrument = _instrument(conn)
    archive = EarningsArchiveService(conn)
    released = TODAY - timedelta(days=1)
    archive.capture(instrument, [EarningsDate(date=released)], now=NOW)

    stored = EarningsResultRepo(conn).get(instrument.id, released)
    assert stored is not None
    assert stored.earnings_day_return_pct is None
    assert stored.observed == []


def test_a_rescheduled_future_release_leaves_no_phantom_row(
    conn: duckdb.DuckDBPyConnection,
) -> None:
    """The cost of capturing on announcement: a moved date leaves a row for a
    release that never happened. It is reconciled away — but only because it
    never reported."""
    instrument = _instrument(conn)
    archive = EarningsArchiveService(conn)
    original = TODAY + timedelta(days=6)
    moved = TODAY + timedelta(days=13)

    archive.capture(instrument, [EarningsDate(date=original, eps_estimate=Decimal("1"))], now=NOW)
    archive.capture(instrument, [EarningsDate(date=moved, eps_estimate=Decimal("1"))], now=NOW)

    results = EarningsResultRepo(conn).for_instrument(instrument.id)
    assert [r.released_on for r in results] == [moved]


def test_a_release_that_reported_survives_a_reconcile(conn: duckdb.DuckDBPyConnection) -> None:
    """The safety catch on the reconcile above, aimed at the only case that can
    actually trip it.

    A past row is never at risk — `reconcile_future` filters on `released_on >
    today`. The row in danger is one still dated ahead that has *already* printed
    a number: yfinance publishes an actual against a date that has not yet passed
    in local terms. Dropping that row because the provider reshuffled its list
    would delete a real observation, and this archive's whole value is that it
    never does. `eps_actual IS NULL` is what stops it.
    """
    instrument = _instrument(conn)
    repo = EarningsResultRepo(conn)
    archive = EarningsArchiveService(conn)
    reported_but_dated_ahead = TODAY + timedelta(days=1)

    archive.capture(
        instrument,
        [EarningsDate(date=reported_but_dated_ahead, eps_actual=Decimal("2"))],
        now=NOW,
    )
    # The provider now lists a different date entirely — a reconcile that keyed
    # on anything but eps_actual would take the reported row with it.
    repo.reconcile_future(instrument.id, [TODAY + timedelta(days=90)], today=TODAY)

    assert repo.get(instrument.id, reported_but_dated_ahead) is not None


# ------------------------------------------------------------ sync integration


def test_sync_fills_the_archive(
    portfolio: PortfolioService, sync_service: SyncService, provider, conn
) -> None:
    """The archive is only worth anything if it accumulates without being asked.
    Every sync that runs without capturing is a quarter permanently lost."""
    portfolio.record_trade("main", "AAPL", Side.BUY, Decimal(1), Decimal(100))
    released = date.today() - timedelta(days=5)
    provider.set_earnings(
        "AAPL",
        [
            EarningsDate(date=released, eps_estimate=Decimal("1.40"), eps_actual=Decimal("1.55")),
            EarningsDate(date=date.today() + timedelta(days=80), eps_estimate=Decimal("1.60")),
        ],
    )

    sync_service.sync()

    aapl = InstrumentRepo(conn).get_by_symbol("AAPL")
    assert aapl is not None
    results = EarningsResultRepo(conn).for_instrument(aapl.id)
    assert len(results) == 2
    reported = next(r for r in results if r.released_on == released)
    assert reported.eps_estimate_pre_release == Decimal("1.40")
    assert reported.eps_surprise_pct == pytest.approx(10.714285, rel=1e-4)
    assert reported.source == "yfinance"
