"""Am I up or down, and what am I risking — the two questions `status` could not answer.

Both used to need a throwaway script against the repos. `status` showed only
unrealized and `report` only realized, so "up or down" meant reading two commands
and doing the arithmetic; money at risk was reported nowhere at all.

The subtlety is that neither number is the obvious one. Realized has to include
partial exits taken out of positions that are still open, or a trim's cash falls
between the two commands and vanishes. Risk is measured from the *mark* against
the stop actually in force, not from entry against the initial stop — a winner
with its trail up risks giving back the gain, not the original stake.
"""

from datetime import datetime
from decimal import Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.cli.render import engine_positions_table, engine_status_renderables
from trd.learn import GlossaryEntry, lookup
from trd.models import EnginePosition, Instrument, InstrumentType, PositionRow, PositionStatus
from trd.services import EngineService


@pytest.fixture
def held(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    """An engine holding one open AAA position, marked below its last close."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    assert len(engine.scan().opened) == 1
    return engine


def row(
    entry: str,
    stop: str,
    mark: str | None,
    quantity: str = "10",
    trail_high: str | None = None,
    # Wide enough that the chandelier stop starts well below the initial one, so
    # a case that does not mention the trail is testing the initial stop.
    atr: str = "5",
    closed_quantity: str = "0",
    status: PositionStatus = PositionStatus.OPEN,
) -> PositionRow:
    """A PositionRow assembled by hand, so the arithmetic is readable in one place."""
    position = EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy="momentum",
        opened_at=datetime(2026, 7, 1, 9, 30),
        entry_price=Decimal(entry),
        quantity=Decimal(quantity),
        stop_price=Decimal(stop),
        target_price=Decimal(entry) * 2,
        atr_at_entry=Decimal(atr),
        trail_high=Decimal(trail_high or entry),
        closed_quantity=Decimal(closed_quantity),
        status=status,
        exit_price=Decimal(mark) if status == PositionStatus.CLOSED and mark else None,
    )
    return PositionRow(
        position=position,
        instrument=Instrument(id=1, symbol="AAA", type=InstrumentType.STOCK),
        price=Decimal(mark) if mark is not None else None,
    )


# ------------------------------------------------------- risk, one position at a time


def test_risk_is_the_distance_to_the_stop_not_the_money_committed() -> None:
    """The whole point. $1,000 committed is not $1,000 at risk — there is a stop
    under it, and that is what turns a scary number into a real one."""
    r = row(entry="100", stop="94", mark="100", quantity="10")
    assert r.position.cost == Decimal("1000")
    assert r.risk_at_stop == Decimal("60")


def test_risk_is_measured_from_the_mark_not_from_entry() -> None:
    """A trade already up is risking the gain as well as the stake, and a trade
    already down has less left to lose. Entry is history; the mark is the money."""
    assert row(entry="100", stop="94", mark="110").risk_at_stop == Decimal("160")
    assert row(entry="100", stop="94", mark="96").risk_at_stop == Decimal("20")


def test_the_trailing_stop_shrinks_the_risk_once_it_takes_over() -> None:
    """Risk must follow the stop actually in force. A chandelier stop above the
    initial one is exactly the case where the initial stop overstates the danger —
    on the live book that gap was a quarter of the total."""
    r = row(entry="100", stop="94", mark="120", trail_high="126", atr="2")
    assert r.trail_stop == Decimal("120")  # 126 - 3 x 2
    assert r.stop_in_force == Decimal("120")
    assert r.risk_at_stop == Decimal("0")

    # Below the initial stop the trail is inert and must not loosen anything.
    early = row(entry="100", stop="94", mark="101", trail_high="99", atr="2")
    assert early.trail_stop == Decimal("93")  # 99 - 3 x 2
    assert early.stop_in_force == Decimal("94")
    assert early.risk_at_stop == Decimal("70")


def test_risk_never_goes_negative() -> None:
    """A position trading through its own stop between scans has no protected
    downside left to report. A negative 'risk' would quietly cancel out real risk
    elsewhere in the book total, which is worse than saying zero."""
    assert row(entry="100", stop="94", mark="90").risk_at_stop == Decimal("0")


def test_risk_counts_only_what_is_still_held() -> None:
    """After a trim, the money at risk is what is left running — the cash already
    taken is booked and cannot be lost to a stop."""
    trimmed = row(entry="100", stop="94", mark="100", quantity="10", closed_quantity="6")
    assert trimmed.position.remaining_quantity == Decimal("4")
    assert trimmed.risk_at_stop == Decimal("24")


def test_a_closed_trade_and_an_unpriced_one_report_no_risk() -> None:
    """None, not zero: 'nothing at risk' and 'cannot say' are different answers,
    and only one of them should be summed into a total."""
    closed = row(entry="100", stop="94", mark="105", status=PositionStatus.CLOSED)
    assert closed.risk_at_stop is None
    assert row(entry="100", stop="94", mark=None).risk_at_stop is None


# --------------------------------------------------------------- the book totals


def test_status_reports_realized_unrealized_and_the_net(held) -> None:
    """All three, never the total alone: an engine up only because of its open
    positions, while its completed trades lost money, is a different engine."""
    position = held.position_rows()[0].position
    entry, risk = position.entry_price, position.risk_per_share

    # One completed loser, so realized and unrealized point opposite ways.
    held.positions.close(position.id, position.opened_at, entry - risk, "stopped out")

    status = held.status()
    assert status.closed_trades == 1
    assert status.open_positions == 0
    cents = Decimal("0.01")
    assert status.realized.quantize(cents) == (-risk * position.quantity).quantize(cents)
    assert status.realized < 0  # exactly -1R, the trade the stop is there for
    assert status.unrealized == Decimal(0)
    assert status.net_pnl == status.realized + status.unrealized


def test_realized_includes_cash_trimmed_out_of_a_still_open_position(held) -> None:
    """The gap a partial exit falls through. `unrealized` prices only what is
    left, and the position is not in the closed set — without this the trimmed
    cash appears in no total anywhere."""
    position = held.position_rows()[0].position
    entry, risk = position.entry_price, position.risk_per_share
    half = position.quantity / 2

    held.positions.trim(position.id, half, entry + risk * 2)

    status = held.status()
    assert status.closed_trades == 0  # still open
    assert status.open_positions == 1
    cents = Decimal("0.01")
    assert status.realized.quantize(cents) == ((risk * 2) * half).quantize(cents)
    assert status.realized > 0
    assert status.net_pnl == status.realized + status.unrealized


def test_status_risk_matches_what_the_positions_table_shows(held) -> None:
    """One computation, two screens. A total that disagreed with the column it
    sums would send someone back to a Python script, which is the bypass this
    ticket exists to close."""
    status = held.status()
    # Marked the way status marks: at the last stored close, no network. Handing
    # in an empty quote map is what makes position_rows fall back to it.
    rows = held.position_rows(quotes={})
    assert status.risk_at_stop == sum((r.risk_at_stop or Decimal(0) for r in rows), Decimal(0))
    assert status.risk_at_stop > 0
    # And it is a fraction of the committed capital, not a rounding of it.
    assert status.risk_at_stop < status.committed / 2


def test_an_empty_book_risks_nothing(
    conn: duckdb.DuckDBPyConnection, provider: FakeProvider
) -> None:
    engine = EngineService(conn, provider)
    provider.add_symbol("AAA", price="100")
    engine.init(symbols=["AAA"], strategies=["breakout"])
    status = engine.status()
    assert status.risk_at_stop == Decimal(0)
    assert status.realized == Decimal(0)
    assert status.net_pnl == Decimal(0)


# ------------------------------------------------------------------ on the screen


def test_the_status_screen_says_all_of_it(held) -> None:
    from rich.console import Console

    console = Console(width=200, record=True)
    for renderable in engine_status_renderables(held.status()):
        console.print(renderable)
    text = console.export_text()
    for label in ("at risk", "realized", "unrealized", "net", "% of committed"):
        assert label in text, f"{label} missing from engine status"


def test_the_positions_table_has_a_risk_column_and_a_book_total(held) -> None:
    from rich.console import Console

    console = Console(width=200, record=True)
    console.print(engine_positions_table(held.position_rows(), "Open", max_positions=5))
    text = console.export_text()
    assert "Risk" in text
    assert "at risk" in text  # the caption total


def test_the_risk_column_survives_json(held) -> None:
    """`--json` dumps the model. A plain @property would print in the table and be
    absent from the document — the failure mode test_json_contract guards."""
    dumped = held.position_rows()[0].model_dump()
    for key in ("risk_at_stop", "stop_in_force", "trail_stop", "mark"):
        assert key in dumped, f"{key} missing from PositionRow JSON"

    status = held.status().model_dump()
    for key in ("risk_at_stop", "realized", "net_pnl", "closed_trades"):
        assert key in status, f"{key} missing from EngineStatus JSON"


def test_learn_defines_money_at_risk_against_committed() -> None:
    """The difference between the two written down once, where it can be looked up."""
    entry = lookup("risk-at-stop")
    assert isinstance(entry, GlossaryEntry)
    assert "committed" in entry.definition.lower()
    assert entry.formula is not None and "stop in force" in entry.formula
