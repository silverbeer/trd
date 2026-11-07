"""Engine scorecard and entry-edge panels (SB-1591). Synthetic numbers."""

from decimal import Decimal

from rich.console import Console

from trd.cli.render import edge_summary, engine_report_summary
from trd.models.engine import StrategyStat
from trd.services.edge import EdgeGroup, EdgeReport, Measure

D = Decimal


def text(renderable) -> str:
    console = Console(width=160, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def stat(name: str, wins: int, losses: int, pnl: str, avg_r: str) -> StrategyStat:
    return StrategyStat(
        strategy=name,
        trades=wins + losses,
        wins=wins,
        losses=losses,
        total_pnl=D(pnl),
        avg_r=D(avg_r),
    )


def test_scorecard_weights_expectancy_by_trades_and_flags_a_small_sample() -> None:
    out = text(engine_report_summary([stat("a", 6, 2, "40", "0.5"), stat("b", 0, 2, "-10", "-1")]))
    # (0.5 x 8 + -1 x 2) / 10 = +0.20R
    assert "+0.20R per trade" in out and "6W 4L" in out and "+30.00" in out
    assert "under 20, read every number as a hypothesis" in out
    assert out.index("─ a ") < out.index("─ b ")  # best expectancy first


def test_scorecard_drops_the_warning_at_twenty_trades() -> None:
    out = text(engine_report_summary([stat("a", 12, 8, "5", "0.1")]))
    assert "hypothesis" not in out


def group(label: str, n: int, edge: float, se: float) -> EdgeGroup:
    m = Measure(edge=edge, edge_se=se)
    flat = Measure()
    return EdgeGroup(
        label=label,
        signals=n,
        mfe_r=flat,
        mae_r=flat,
        net_r=m,
        forward_r=flat,
        target_first_pct=flat,
        stop_first_pct=flat,
    )


def test_edge_verdicts_and_small_samples() -> None:
    report = EdgeReport(
        timeframe="1d",
        samples=20,
        seed=1,
        horizon_bars=5,
        taken_signals=40,
        measured=40,
        unmeasured=0,
        overall=group("all taken", 40, 0.5, 0.1),
        by_strategy=[group("good", 30, 0.6, 0.1), group("tiny", 4, -1.5, 0.3)],
        by_time=[],
    )
    out = text(edge_summary(report))
    assert "✓ beats random" in out and "✗ worse than random" in out
    assert "n < 20: a hypothesis" in out
    assert out.count("n < 20") == 1  # only the four-signal card
