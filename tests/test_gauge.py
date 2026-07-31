"""The R gauge: where a trade sits between the stop it dies at and the target."""

from decimal import Decimal

from rich.console import Console

from trd.cli.render import r_gauge

ENTRY, STOP, TARGET = Decimal("100"), Decimal("90"), Decimal("120")


def _plain(markup: str) -> str:
    console = Console(width=200, no_color=True, record=True)
    console.print(markup, end="")
    return console.export_text(styles=False).rstrip("\n")


def test_marker_tracks_price_across_the_range():
    at_stop = _plain(r_gauge(ENTRY, STOP, STOP, TARGET))
    at_entry = _plain(r_gauge(ENTRY, ENTRY, STOP, TARGET))
    at_target = _plain(r_gauge(ENTRY, TARGET, STOP, TARGET))
    assert at_stop.index("●") < at_entry.index("●") < at_target.index("●")


def test_entry_is_not_in_the_middle_and_that_is_the_point():
    """On a 2R trade the entry sits a third of the way along, because the scale
    is the trade's own risk. Seeing that is half the value of the gauge."""
    bar = _plain(r_gauge(ENTRY, Decimal("115"), STOP, TARGET, width=13))
    entry_at = bar.index("┼")
    assert 3 <= entry_at <= 6  # roughly a third along a 15-cell rendering
    assert entry_at < bar.index("●")


def test_price_through_the_stop_is_marked_not_misplaced():
    """A gap through the stop is a real state. Clamp the marker, but say so —
    drawing it inside the range would be a lie."""
    bar = _plain(r_gauge(ENTRY, Decimal("70"), STOP, TARGET))
    assert "◀" in bar
    assert "●" not in bar


def test_price_past_the_target_is_marked_too():
    bar = _plain(r_gauge(ENTRY, Decimal("200"), STOP, TARGET))
    assert "▶" in bar
    assert "●" not in bar


def test_colour_tracks_distance_above_the_stop_not_the_sign_of_pnl():
    """A trade barely above its stop needs attention whether or not it is green."""
    near_stop = r_gauge(ENTRY, Decimal("91"), STOP, TARGET)
    comfortable = r_gauge(ENTRY, Decimal("115"), STOP, TARGET)
    assert "bright_red" in near_stop
    assert "green" in comfortable


def test_a_missing_price_renders_an_empty_track_not_a_wrong_one():
    bar = _plain(r_gauge(ENTRY, None, STOP, TARGET))
    assert "●" not in bar and "◀" not in bar and "▶" not in bar


def test_degrades_without_colour():
    """NO_COLOR must lose the colour, never the information."""
    bar = _plain(r_gauge(ENTRY, Decimal("115"), STOP, TARGET))
    assert "●" in bar and "┼" in bar and bar.startswith("├") and bar.endswith("┤")


def test_uses_only_box_drawing_characters():
    """trend_change avoids block glyphs deliberately — they render inconsistently
    across fonts. The gauge must not reintroduce that."""
    bar = _plain(r_gauge(ENTRY, Decimal("115"), STOP, TARGET))
    blocks = set("▁▂▃▄▅▆▇█▏▎▍▌▋▊▉")
    assert not (set(bar) & blocks)


def test_a_degenerate_range_does_not_crash():
    assert "─" in _plain(r_gauge(ENTRY, ENTRY, Decimal("120"), Decimal("120")))


def test_the_gauge_is_dropped_before_the_table_truncates():
    """Decoration goes first when space runs short. A truncated table loses data
    silently, which is worse than losing a second reading of numbers already in
    the row — that is the whole reason SB-459 exists."""
    from datetime import datetime

    from trd.cli.render import engine_positions_table
    from trd.models import EnginePosition, Instrument, InstrumentType, PositionRow

    position = EnginePosition(
        id=1,
        account_id=1,
        instrument_id=1,
        strategy="pullback",
        opened_at=datetime(2026, 7, 30, 9, 30),
        entry_price=ENTRY,
        quantity=Decimal("2"),
        stop_price=STOP,
        target_price=TARGET,
        atr_at_entry=Decimal("5"),
        trail_high=ENTRY,
    )
    row = PositionRow(
        position=position,
        instrument=Instrument(id=1, symbol="AAA", name="AAA", type=InstrumentType.STOCK),
        price=Decimal("110"),
    )

    def headers(width: int) -> list[str]:
        table = engine_positions_table([row], "t", terminal_width=width)
        return [str(c.header) for c in table.columns]

    assert "stop → target" in headers(200)
    assert "stop → target" not in headers(95)
    assert "R" in headers(95)  # the data survives
