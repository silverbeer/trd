"""The one-command sweep (SB-1345): axes parsed one knob at a time, every row
carried with its error bars and its split-half check."""

import json
import os
from datetime import timedelta
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from tests.conftest import FakeProvider
from tests.test_engine import make_bars, seed, uptrend
from trd.cli.app import app
from trd.errors import TrdError
from trd.services import EngineService
from trd.services.sweep import SweepRow, compare, parse_axes

runner = CliRunner()


def row(mean: float, se: float, first: float, second: float) -> SweepRow:
    return SweepRow(
        label="x",
        trades=100,
        expectancy_r=mean,
        se_r=se,
        max_drawdown_pct=-5.0,
        first_half_r=first,
        second_half_r=second,
    )


def test_each_value_is_a_row_and_each_flag_an_axis() -> None:
    assert parse_axes(["entry_delay_minutes=5,10", "target_r=1.5"]) == [
        {"entry_delay_minutes": 5.0},
        {"entry_delay_minutes": 10.0},
        {"target_r": 1.5},
    ]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "Nothing to sweep"),
        (["trail_mult=2"], "Unknown param"),
        (["target_r=x"], "not a number"),
        (["target_r=-1"], "cannot be negative"),
        (["target_r"], "Expected KEY=VALUE"),
    ],
)
def test_a_sweep_that_would_report_the_baseline_is_refused(raw, message) -> None:
    with pytest.raises(TrdError, match=message):
        parse_axes(raw)


def test_a_row_carried_by_one_half_is_flagged_and_never_significant() -> None:
    """entry_delay_minutes=15 in SB-1033: the only positive row, every bit of it
    from the second half. Its t can be large; it still does not clear the bar."""
    base = row(0.0, 0.01, 0.0, 0.0)
    one_half = compare(base, row(0.2, 0.01, -0.1, 0.5))
    assert one_half.diff_t is not None and one_half.diff_t > 2
    assert one_half.halves_disagree is True
    assert one_half.significant is False


def test_a_row_better_in_both_halves_clears_the_bar() -> None:
    base = row(0.0, 0.01, 0.0, 0.0)
    both = compare(base, row(0.1, 0.01, 0.08, 0.12))
    assert both.halves_disagree is False
    assert both.significant is True
    assert both.diff_se == pytest.approx((0.01**2 + 0.01**2) ** 0.5)


def test_cli_sweeps_against_the_stored_config(cli_env: FakeProvider, tmp_path) -> None:
    from trd.db.connection import connect

    home = tmp_path / "engine"
    home.mkdir()
    conn = connect(home / "trd.duckdb")
    bars = make_bars(uptrend())
    seed(conn, "AAA", bars)
    cli_env.add_symbol("AAA", price=str(float(bars[-1].close) * 0.998), volume=1_200_000)
    EngineService(conn, cli_env).init(
        symbols=["AAA"], strategies=["momentum"], position_size=Decimal("10000")
    )
    conn.close()

    os.environ["TRD_HOME"] = str(home)
    try:
        result = runner.invoke(
            app, ["engine", "sweep", "-p", "target_r=1.5,3", "--workers", "1", "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["baseline"]["label"] == "baseline"
        assert [r["label"] for r in payload["rows"]] == ["target_r=1.5", "target_r=3"]
        assert payload["split"] > payload["start"]
        assert payload["end"] >= str(bars[-1].date - timedelta(days=1))
    finally:
        os.environ.pop("TRD_HOME", None)
