"""The point-in-time earnings archive.

`earnings_event` answers "is a report close?" — it holds a date, it is rewritten
on every sync, and that is exactly right for the entry blackout. It is useless as
*evidence*, because it cannot say what was known before a release. An estimate
read after the fact is not a consensus; it is a memory of one.

This service writes the other kind of record: what the tape knew, when it knew
it, written once and never revised. Two rules follow from that and everything
here is shaped by them.

**Capture on announcement, not on report.** A row is written when a release is
first seen, including a future one, because the estimate is only point-in-time
before the number lands. Waiting until a report has printed would archive the
post-release estimate — the corruption the table exists to prevent. `eps_actual`
and the reaction arrive later and fill fields that were genuinely unknown.

**Never claim more than was observed.** yfinance supplies a date, an estimate and
a reported EPS. It does not supply revenue, guidance, analyst revisions, or
BMO/AMC release timing. Those columns stay NULL and `quality_status` names what
was measured, so a missing revenue surprise can never be read downstream as a
surprise of zero.

The reason this ships ahead of anything that consumes it: the history cannot be
bought back. yfinance serves roughly twelve quarters and reports only today's
values, so every sync that runs without this archive is a quarter of
point-in-time data permanently lost.
"""

from datetime import date, datetime

import duckdb

from trd.models import Bar, EarningsDate, EarningsResult, Instrument
from trd.repos import EarningsResultRepo, InstrumentRepo, PriceRepo

SOURCE = "yfinance"

# The tape a reaction is measured against. Same symbol the regime gate uses, so
# "relative strength" means one thing across the codebase.
BENCHMARK_SYMBOL = "SPY"


class EarningsArchiveService:
    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        self.results = EarningsResultRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)

    def capture(
        self,
        instrument: Instrument,
        events: list[EarningsDate],
        now: datetime | None = None,
    ) -> int:
        """Archive what this sync observed about one instrument's releases.

        Returns the number of rows written or filled. Safe to run on every sync:
        an existing row is only ever *completed*, never revised.
        """
        if not events:
            return 0
        now = now or datetime.now()
        today = now.date()
        touched = 0
        for event in events:
            if self.results.record(self._first_sighting(instrument.id, event, now)):
                touched += 1
                continue
            # Already on record. Only fields that could not have existed then.
            if self.results.fill_unknown(instrument.id, event.date, eps_actual=event.eps_actual):
                touched += 1
        touched += self._fill_reactions(instrument, events, today)
        self.results.reconcile_future(instrument.id, [e.date for e in events], today)
        return touched

    # ------------------------------------------------------------- internals

    def _first_sighting(
        self, instrument_id: int, event: EarningsDate, now: datetime
    ) -> EarningsResult:
        observed = []
        if event.eps_estimate is not None:
            observed.append("estimate")
        if event.eps_actual is not None:
            observed.append("eps")
        return EarningsResult(
            instrument_id=instrument_id,
            released_on=event.date,
            # yfinance publishes no release time. Recording 'unknown' rather than
            # guessing is what forces a consumer onto the conservative
            # next-session path instead of a BMO fast path it cannot justify.
            release_timing="unknown",
            source=SOURCE,
            source_observed_at=now,
            eps_actual=event.eps_actual,
            eps_estimate_pre_release=event.eps_estimate,
            quality_status=",".join(sorted(observed)),
        )

    def _fill_reactions(
        self, instrument: Instrument, events: list[EarningsDate], today: date
    ) -> int:
        """Freeze the earnings-session move once its daily bar has settled."""
        released = [e.date for e in events if e.date <= today]
        if not released:
            return 0
        bars = self.prices.daily_bars(instrument.id)
        if len(bars) < 2:
            return 0
        benchmark = self._benchmark_bars()
        filled = 0
        for released_on in released:
            move = _session_return(bars, released_on)
            if move is None:
                continue
            relative = None
            if benchmark is not None:
                tape = _session_return(benchmark, released_on)
                relative = None if tape is None else move - tape
            if self.results.fill_unknown(
                instrument.id,
                released_on,
                earnings_day_return_pct=move,
                earnings_day_relative_strength_pct=relative,
            ):
                filled += 1
        return filled

    def _benchmark_bars(self) -> list[Bar] | None:
        found = self.instruments.get_by_symbol(BENCHMARK_SYMBOL)
        if found is None:
            return None
        bars = self.prices.daily_bars(found.id)
        return list(bars) if bars else None


def _session_return(bars: list, released_on: date) -> float | None:
    """Percent move of the first settled session on or after `released_on`.

    On or after, because a release date is not always a trading day, and because
    without BMO/AMC timing there is no way to tell whether the reaction belongs
    to that session or the next. The row's `release_timing` is 'unknown', which
    is where that ambiguity is recorded — this number is the move of the session
    the market first had to price the report in, not a claim about which one the
    report landed inside.

    None until the bar exists and has a predecessor: a reaction is a comparison,
    and there is nothing to compare the very first stored bar against.
    """
    for i, bar in enumerate(bars):
        if bar.date < released_on:
            continue
        if i == 0:
            return None
        prev = bars[i - 1].close
        if prev == 0:
            return None
        return float((bar.close - prev) / prev * 100)
    return None
