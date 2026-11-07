"""The rich UI kit (SB-1587): the parts every human view is built from."""

from decimal import Decimal

from rich.console import Console

from trd.cli.ui import (
    card,
    cards,
    change_line,
    dashboard,
    gauge,
    hero,
    severity,
    sparkline,
    tone,
    versus,
    win_loss,
)

D = Decimal


def text(renderable, width: int = 120) -> str:
    console = Console(width=width, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_gauge_clamps_and_fills_proportionally() -> None:
    assert gauge(0.5, 10).count("━") == 10
    assert "[green]━━━━━[/green]" in gauge(0.5, 10)
    assert "[green][/green]" in gauge(-1, 4) and "[green]━━━━[/green]" in gauge(9, 4)


def test_sparkline_colours_rises_falls_and_the_peak() -> None:
    line = sparkline([D(1), D(3), D(2)], peak_at=1, target=3)
    assert line.plain == "▁█▄"
    styles = [str(span.style) for span in line.spans]
    assert styles == ["green", "bold gold1", "red"]
    assert sparkline([]).plain == ""
    assert sparkline([D(5), D(5)], target=2).plain == "▁▁"  # flat line, no division


def test_tone_and_severity() -> None:
    assert tone(D(0)) == ("green", "▲") and tone(D("-0.01")) == ("red", "▼")
    assert tone(None)[0] == "dim"
    assert [severity(v, 5, 15) for v in (D(-2), D(-9), D(-30))] == ["green", "yellow", "red"]


def test_change_line_and_versus() -> None:
    assert "100.00  →  97.50   ▼ -2.50  (-2.50%)" in change_line(D(100), D("97.5")).plain
    assert "▲ +10.00" in change_line(D(100), D(110)).plain
    ahead, behind = versus("SPY", D("-2.50"), D("-3.74")), versus("SPY", D(1), D(4))
    assert ahead is not None and "1.24 pts ahead" in ahead.plain
    assert behind is not None and "3.00 pts behind" in behind.plain
    assert versus("SPY", D(1), None) is None


def test_win_loss_row() -> None:
    label, value = win_loss(5, 8)
    assert "5W" in label and "8L" in label and value.endswith("38%")
    assert win_loss(0, 0) == ("closed", "[dim]none yet[/dim]")


def test_cards_share_a_height_and_never_cut_a_value() -> None:
    row = cards(
        ("A", [("one", "1")], "cyan"),
        ("B", [("x", "1"), ("y", "2"), ("z", "a long value nobody may truncate")], "blue"),
        ("Empty", [], "red"),
    )
    narrow = text(row, width=40)
    assert "a long value nobody may truncate" in narrow
    assert "Empty" not in narrow  # an empty card is dropped
    assert card("T", [("k", "v")], "cyan", height=5).height == 5


def test_dashboard_skips_missing_hero_lines() -> None:
    out = text(
        dashboard(
            hero("Title", [change_line(D(1), D(2)), None], subtitle="since"),
            ("Card", [("k", "v")], "cyan"),
        )
    )
    assert "✦ Title · since" in out and "Card" in out
