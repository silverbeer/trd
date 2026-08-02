"""Every computed value a model exposes must survive `--json`.

The output contract promises "the underlying model, full precision, stable keys".
A plain `@property` breaks that silently: pydantic does not serialise it, so the
field reads fine in Python, prints fine in the Rich table, and is simply absent
from the document an agent parses. Nothing errors.

That is how `entry_date`/`exit_date` vanished in #77 and how `total_return_pct`,
`expectancy_r`, `win_rate`, `unrealized_pl` and 24 others were never there at all.
`test_no_model_property_is_missing_from_json` is the guard: it fails on the next
plain `@property` added to a serialised model, rather than waiting for someone to
notice a missing key months later.
"""

import ast
import pathlib
from datetime import date, datetime
from decimal import Decimal

import pytest

from trd.models import (
    EnginePosition,
    Instrument,
    InstrumentType,
    PositionStatus,
    Quote,
    StrategyStat,
)

SERIALISED_SOURCES = [
    "src/trd/models/core.py",
    "src/trd/models/engine.py",
    "src/trd/services/backtest.py",
]


def _plain_properties() -> list[tuple[str, str, str]]:
    """(file, class, property) for every un-serialised property on a BaseModel."""
    found = []
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in SERIALISED_SOURCES:
        tree = ast.parse((root / rel).read_text())
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            bases = {getattr(b, "id", getattr(b, "attr", "")) for b in cls.bases}
            if "BaseModel" not in bases:
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef):
                    continue
                decs = {ast.unparse(d) for d in fn.decorator_list}
                if "property" in decs and "computed_field" not in decs:
                    found.append((rel, cls.name, fn.name))
    return found


def test_no_model_property_is_missing_from_json() -> None:
    """The guard. A plain @property on a serialised model is invisible in --json.

    If this fails, the fix is `@computed_field` above the `@property` — not an
    entry in an exclusion list. A value worth computing for the table is worth
    handing to whatever is parsing the JSON.
    """
    plain = _plain_properties()
    assert plain == [], "properties missing from --json: " + ", ".join(
        f"{cls}.{prop} ({f})" for f, cls, prop in plain
    )


def test_the_check_can_actually_fail() -> None:
    """A guard that cannot fail is decoration. Proves the AST walk finds one."""
    src = (
        "from pydantic import BaseModel\n\n"
        "class M(BaseModel):\n"
        "    @property\n"
        "    def x(self) -> int:\n"
        "        return 1\n"
    )
    tree = ast.parse(src)
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef))
    decs = {ast.unparse(d) for d in fn.decorator_list}
    assert "property" in decs and "computed_field" not in decs


# ------------------------------------------------- the values, actually present


def test_strategy_stat_carries_the_headline_numbers() -> None:
    """`expectancy_r` is the number the scorecard leads with, and it was absent."""
    stat = StrategyStat(
        strategy="breakout",
        trades=10,
        wins=6,
        losses=4,
        total_pnl=Decimal("120"),
        avg_win_pct=Decimal("3"),
        avg_loss_pct=Decimal("-1.5"),
        avg_r=Decimal("0.28"),
        open_trades=1,
    )
    dumped = stat.model_dump()
    assert dumped["win_rate"] is not None
    assert dumped["expectancy_r"] is not None


def test_engine_position_carries_its_derived_money() -> None:
    position = EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy="breakout",
        opened_at=datetime(2026, 7, 1, 10, 0),
        entry_price=Decimal("100"),
        quantity=Decimal("10"),
        stop_price=Decimal("95"),
        target_price=Decimal("110"),
        atr_at_entry=Decimal("2.5"),
        trail_high=Decimal("104"),
        status=PositionStatus.OPEN,
        last_bar_date=date(2026, 7, 2),
    )
    dumped = position.model_dump()
    for key in ("risk_per_share", "cost", "realized_pnl", "realized_r"):
        assert key in dumped, f"{key} missing"
    assert dumped["cost"] == Decimal("1000")
    assert dumped["risk_per_share"] == Decimal("5")


def test_quote_carries_its_derived_percentages() -> None:
    quote = Quote(symbol="AAA", price=Decimal("110"), prev_close=Decimal("100"))
    dumped = quote.model_dump()
    assert dumped["day_change"] == Decimal("10")
    assert dumped["day_change_pct"] is not None


@pytest.mark.parametrize("key", ["market_value", "unrealized_pl", "unrealized_pl_pct", "avg_cost"])
def test_portfolio_position_carries_its_pnl(key: str) -> None:
    """`trd portfolio --json` returned cost and quantity but no profit or loss —
    the reader had to recompute what the table already printed."""
    from trd.models import Position

    position = Position(
        instrument=Instrument(id=1, symbol="AAA", type=InstrumentType.STOCK),
        quantity=Decimal("10"),
        cost_basis=Decimal("1000"),
        price=Decimal("120"),
    )
    assert key in position.model_dump()


def test_json_round_trips_through_model_dump_json() -> None:
    """model_dump() and model_dump_json() must agree — the CLI uses the latter."""
    import json

    quote = Quote(symbol="AAA", price=Decimal("110"), prev_close=Decimal("100"))
    assert set(json.loads(quote.model_dump_json())) == set(quote.model_dump())
