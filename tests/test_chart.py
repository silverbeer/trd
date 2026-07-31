"""The equity line chart: shape, baseline, and an honest drawdown."""

from rich.console import Console

from trd.cli.render import line_chart


def _plain(renderable) -> str:
    console = Console(width=200, no_color=True, record=True)
    console.print(renderable, end="")
    return console.export_text(styles=False)


def test_a_rising_series_ends_higher_than_it_starts():
    lines = _plain(line_chart([10.0, 20.0, 30.0], "a", "b", width=3, height=5)).splitlines()
    rows = [ln for ln in lines if "─" in ln or "│" in ln]
    first_col = [i for i, ln in enumerate(rows) if len(ln) > 13 and ln[13] != " "]
    last_col = [i for i, ln in enumerate(rows) if len(ln) > 15 and ln[15] != " "]
    assert min(last_col) < min(first_col)  # the end sits higher on the screen


def test_the_line_is_connected_not_a_scatter():
    """A jump between adjacent points must be joined, or the eye has to assemble
    a line out of dots."""
    out = _plain(line_chart([0.0, 100.0], "a", "b", width=2, height=8))
    assert "│" in out


def test_the_baseline_marks_where_the_series_started():
    """Above or below water should be a glance, not an arithmetic problem."""
    assert "┄" in _plain(line_chart([100.0, 120.0], "a", "b", baseline=100.0))
    assert "┄" not in _plain(line_chart([100.0, 120.0], "a", "b"))


def test_drawdown_is_measured_on_the_full_series_not_the_downsampled_copy():
    """Measuring the drawn copy would print a different drawdown from the one the
    summary reports, for the same curve."""
    # A single deep spike down, surrounded by flat values, in a long series.
    values = [100.0] * 200 + [50.0] + [100.0] * 200
    out = _plain(line_chart(values, "a", "b", width=20))
    assert "-50.0%" in out  # the spike survives, though 401 points map to 20 columns


def test_a_curve_that_only_rises_reports_no_drawdown():
    out = _plain(line_chart([10.0, 20.0, 30.0, 40.0], "a", "b"))
    assert "deepest drawdown" not in out


def test_axis_labels_bracket_the_series():
    out = _plain(line_chart([10.0, 90.0], "2024-01-01", "2026-01-01"))
    assert "2024-01-01" in out and "2026-01-01" in out
    assert "90" in out and "10" in out


def test_empty_series_does_not_crash():
    assert _plain(line_chart([], "a", "b")).strip() == ""


def test_a_flat_series_does_not_divide_by_zero():
    out = _plain(line_chart([50.0, 50.0, 50.0], "a", "b"))
    assert "50" in out


def test_uses_only_box_drawing_characters():
    out = _plain(line_chart([10.0, 50.0, 20.0, 60.0], "a", "b", baseline=10.0))
    blocks = set("▁▂▃▄▅▆▇█▏▎▍▌▋▊▉")
    assert not (set(out) & blocks)
