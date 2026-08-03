"""Partial exits: taking cash out of a live position without abandoning it.

`trd sell` cannot do this — it leaves `engine_position` claiming the original size
and the engine later sells all of it, taking the account short. That path is now
refused; this is the operation that replaces it.

The subtle part is R. A trade is measured against the risk it took at entry, so
`quantity` stays the original size and every partial books against that same
denominator. Sell 90% at +2R, stop the last 10% at -1R, and the trade reports
+1.7R — one number for one trade. `test_r_multiple_weighs_each_piece` is that
arithmetic, and it is the test worth reading.
"""

from decimal import ROUND_DOWN, Decimal

import duckdb
import pytest

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.errors import TrdError
from trd.models import PositionStatus
from trd.services import EngineService


@pytest.fixture
def held(conn: duckdb.DuckDBPyConnection, provider: FakeProvider) -> EngineService:
    """An engine holding one open AAA position."""
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    provider.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    engine = EngineService(conn, provider)
    engine.init(symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000"))
    assert len(engine.scan().opened) == 1
    return engine


def _position(engine: EngineService):
    return engine.position_rows(open_only=True)[0].position


# --------------------------------------------------------------- the arithmetic


def test_r_multiple_weighs_each_piece(held) -> None:
    """The number that makes the scorecard mean anything.

    Sell 90% at +2R and stop the remaining 10% at -1R: the trade booked
    0.9 x 2 + 0.1 x -1 = +1.7R. Not two trades, not the last exit's R — one
    weighted result for one decision.
    """
    position = _position(held)
    entry, risk = position.entry_price, position.risk_per_share

    held.positions.trim(position.id, position.quantity * Decimal("0.9"), entry + risk * 2)
    held.positions.close(
        position.id, position.opened_at, entry - risk, "stopped out on the remainder"
    )

    booked = _closed(held)
    assert booked.realized_r is not None
    assert booked.realized_r.quantize(Decimal("0.01")) == Decimal("1.70")


def _closed(engine: EngineService):
    rows = engine.position_rows(open_only=False)
    return next(r.position for r in rows if r.position.status == PositionStatus.CLOSED)


def test_an_untrimmed_trade_scores_exactly_as_before(held) -> None:
    """The regression guard: single-exit trades must be unchanged."""
    position = _position(held)
    entry, risk = position.entry_price, position.risk_per_share
    held.positions.close(position.id, position.opened_at, entry + risk * 2, "target")

    booked = _closed(held)
    assert booked.realized_r is not None
    assert booked.realized_r.quantize(Decimal("0.0001")) == Decimal("2.0000")
    # booked_pnl is DECIMAL(24,8) in the schema, so compare at the stored precision.
    assert booked.realized_pnl == ((risk * 2) * position.quantity).quantize(Decimal("0.00000001"))


def test_exit_price_is_the_weighted_average_across_pieces(held) -> None:
    position = _position(held)
    half = position.quantity / 2
    held.positions.trim(position.id, half, Decimal("100"))
    held.positions.close(position.id, position.opened_at, Decimal("200"), "target")

    assert _closed(held).exit_price == Decimal("150")


# ------------------------------------------------------------------- the trim


def test_trim_leaves_the_rest_running(held, provider) -> None:
    position = _position(held)
    fill = held.trim("AAA", pct=Decimal("40"), price=Decimal("300"))

    after = _position(held)
    assert after.status == PositionStatus.OPEN
    # ROUND_DOWN, matching the service: a trim must never sell more than asked for.
    expected = (position.quantity * Decimal("0.4")).quantize(
        Decimal("0.000001"), rounding=ROUND_DOWN
    )
    assert fill.quantity == expected
    assert after.remaining_quantity == position.quantity - fill.quantity
    assert after.is_partial is True


def test_trim_does_not_move_the_stop_or_target(held) -> None:
    """Taking money off the table is not a reason to change the plan for the rest."""
    before = _position(held)
    held.trim("AAA", pct=Decimal("50"), price=Decimal("300"))
    after = _position(held)

    assert after.stop_price == before.stop_price
    assert after.target_price == before.target_price
    assert after.quantity == before.quantity  # the R denominator never moves


def test_trim_records_a_real_sell(held, conn) -> None:
    from trd.repos import TransactionRepo

    fill = held.trim("AAA", pct=Decimal("25"), price=Decimal("300"))
    txns = TransactionRepo(conn).list_chronological(held.account().id)
    sells = [t for t in txns if t.side == "sell"]

    assert len(sells) == 1
    assert sells[0].quantity == fill.quantity
    assert "trim" in (sells[0].note or "")


def test_the_engine_then_exits_only_the_remainder(held) -> None:
    """The bug this whole ticket started from: the engine must not sell what it
    no longer holds."""
    position = _position(held)
    held.trim("AAA", pct=Decimal("60"), price=Decimal("300"))
    remaining = _position(held).remaining_quantity

    held.provider.add_symbol("AAA", price=str(float(position.stop_price) * 0.99), volume=1_200_000)
    result = held.scan()

    assert len(result.closed) == 1
    assert result.closed[0].quantity == remaining


def test_books_balance_after_a_trim_and_an_exit(held, conn) -> None:
    """Net position across every txn must be zero — the failure in SB-550 was
    ending up short."""
    position = _position(held)
    held.trim("AAA", pct=Decimal("60"), price=Decimal("300"))
    held.provider.add_symbol("AAA", price=str(float(position.stop_price) * 0.99), volume=1_200_000)
    held.scan()

    row = conn.execute(
        """SELECT sum(CASE WHEN t.side = 'buy' THEN t.quantity ELSE -t.quantity END)
           FROM txn t JOIN instrument i ON i.id = t.instrument_id WHERE i.symbol = 'AAA'"""
    ).fetchone()
    assert row is not None and row[0] == Decimal(0)


# ------------------------------------------------------------------- refusals


def test_trimming_everything_is_refused(held) -> None:
    """Closing should go through an exit rule, so the trade records why it ended."""
    with pytest.raises(TrdError, match="would close the position"):
        held.trim("AAA", quantity=_position(held).remaining_quantity, price=Decimal("300"))


@pytest.mark.parametrize("pct", [Decimal(0), Decimal(100), Decimal(-5), Decimal(140)])
def test_out_of_range_percentages_are_refused(held, pct) -> None:
    with pytest.raises(TrdError, match="between 0 and 100"):
        held.trim("AAA", pct=pct, price=Decimal("300"))


def test_needs_exactly_one_of_pct_or_quantity(held) -> None:
    with pytest.raises(TrdError, match="exactly one"):
        held.trim("AAA", price=Decimal("300"))
    with pytest.raises(TrdError, match="exactly one"):
        held.trim("AAA", pct=Decimal("50"), quantity=Decimal("1"), price=Decimal("300"))


def test_a_symbol_the_engine_does_not_hold_is_refused(held, provider) -> None:
    provider.add_symbol("BBB", price="50")
    held.instruments.insert(provider.get_info("BBB"))  # tracked, but never traded

    with pytest.raises(TrdError, match="no open position"):
        held.trim("BBB", pct=Decimal("50"), price=Decimal("50"))


def test_an_unknown_symbol_says_so(held) -> None:
    with pytest.raises(TrdError, match="Unknown symbol"):
        held.trim("NOPE", pct=Decimal("50"), price=Decimal("50"))
