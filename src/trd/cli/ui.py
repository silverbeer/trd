"""The rich UI kit every human-facing trd view is built from (SB-1587).

One look, shared: a HERO panel that says where the money stands in one glance,
over CARDS of label/value rows laid out side by side. First shipped as the
'Since the start' summary of `trd engine performance` (SB-1586); these are its
parts, so the next view looks like that one instead of like whatever its author
had in mind that day.

Rules the kit enforces so callers don't have to remember them:

- Green is up/good, red is down/bad, gold is a peak, dim is context. A colour
  never carries meaning alone — the sign, arrow or word is always printed too.
- Cards take their natural width and wrap onto a new row on a narrow terminal
  (`rich.columns.Columns`). A value is never truncated to fit.
- Cards in a row share a height, so a row reads as one block.
- Only rendering. `--json` never passes through here — the output contract is
  the model, not the picture.

Money helpers (`fmt_money`, `fmt_signed`, ...) live here too and are
re-exported by `render`, so both modules format a number the same way.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

MONEY = "{:,.2f}"

# A row of a card: a label then one or more values, all Rich markup. Most
# cards are label/value; a card with more columns lines them up as a table.
Row = tuple[str, ...]

UP, DOWN, PEAK, OFF = "green", "red", "gold1", "grey23"
SPARK = "▁▂▃▄▅▆▇█"
GAUGE = "━"


# ----------------------------------------------------------------- formatting


def fmt_money(value: Decimal | None) -> str:
    return MONEY.format(value) if value is not None else "—"


def fmt_qty(value: Decimal) -> str:
    return f"{value.normalize():f}"


def fmt_signed(value: Decimal | None) -> str:
    if value is None:
        return "—"
    color = UP if value >= 0 else DOWN
    return f"[{color}]{'+' if value >= 0 else ''}{MONEY.format(value)}[/{color}]"


def fmt_signed_pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    color = UP if value >= 0 else DOWN
    return f"[{color}]{'+' if value >= 0 else ''}{value:.2f}%[/{color}]"


def tone(value: Decimal | float | None) -> tuple[str, str]:
    """(colour, arrow) for a change: ▲ green at or above zero, ▼ red below, dim unknown."""
    if value is None:
        return "dim", "·"
    return (UP, "▲") if value >= 0 else (DOWN, "▼")


def severity(value: Decimal | float, warn: float, bad: float) -> str:
    """Green under `warn`, yellow under `bad`, red beyond — for a magnitude
    where bigger is worse (a drawdown, a drift, a share of risk)."""
    magnitude = abs(float(value))
    return UP if magnitude < warn else "yellow" if magnitude < bad else DOWN


# ------------------------------------------------------------------ glyphs


def gauge(fraction: float, width: int = 10, on: str = UP, off: str = OFF) -> str:
    """A horizontal bar: `fraction` (clamped to 0..1) of `width` cells in `on`."""
    n = max(0, min(width, round(max(0.0, min(1.0, fraction)) * width)))
    return f"[{on}]{GAUGE * n}[/{on}][{off}]{GAUGE * (width - n)}[/{off}]"


def sparkline(
    values: Sequence[Decimal | float], peak_at: int | None = None, target: int = 40
) -> Text:
    """A line's shape in block glyphs: green where it rose from the point
    before, red where it fell, the `peak_at` point in gold. A short series is
    stretched toward `target` cells — eleven cells of history is a smudge — and
    a long one bucketed down to `target`, so it stays one line."""
    text = Text()
    if not values:
        return text
    points = [float(v) for v in values]
    if len(points) > target:
        # A long history is bucketed to `target` cells, each the bucket's last
        # value; the peak lands in whichever bucket held it.
        size = len(points) / target
        if peak_at is not None:
            peak_at = min(target - 1, int(peak_at / size))
        points = [points[min(len(points) - 1, int((i + 1) * size) - 1)] for i in range(target)]
    lo, hi = min(points), max(points)
    span = hi - lo
    stretch = max(1, min(4, target // len(points)))
    previous = points[0]
    for i, v in enumerate(points):
        level = 0 if span == 0 else int((v - lo) / span * (len(SPARK) - 1))
        style = f"bold {PEAK}" if i == peak_at else UP if v >= previous else DOWN
        text.append(SPARK[level] * stretch, style=style)
        previous = v
    return text


# ------------------------------------------------------------------- lines


def change_line(
    start: Decimal, now: Decimal, *, pct: Decimal | None = None, start_label: str = ""
) -> Text:
    """'start → now ▲ +delta (+pct%)', coloured by the sign of the change."""
    delta = now - start
    if pct is None and start:
        pct = delta / start * 100
    color, arrow = tone(delta)
    label = f"[dim]{start_label}[/dim] " if start_label else ""
    tail = f"  ({pct:+.2f}%)" if pct is not None else ""
    return Text.from_markup(
        f"{label}[dim]{fmt_money(start)}[/dim]  [bold]→[/bold]  "
        f"[bold {color}]{fmt_money(now)}[/bold {color}]   "
        f"[{color}]{arrow} {fmt_signed(delta)}{tail}[/{color}]"
    )


def versus(name: str, own_pct: Decimal | None, bench_pct: Decimal | None) -> Text | None:
    """'vs SPY -3.74%  ✓ 1.24 pts ahead' — None when either side is unknown."""
    if own_pct is None or bench_pct is None:
        return None
    gap = own_pct - bench_pct
    verdict = (
        f"[bold {UP}]✓ {gap:.2f} pts ahead[/bold {UP}]"
        if gap >= 0
        else f"[bold {DOWN}]✗ {-gap:.2f} pts behind[/bold {DOWN}]"
    )
    return Text.from_markup(f"[dim]vs {name}[/dim] {fmt_signed_pct(bench_pct)}  {verdict}")


def win_loss(won: int, lost: int, width: int = 12) -> Row:
    """The W/L row of a trades card: counts, a green-over-red bar, the win rate."""
    total = won + lost
    if not total:
        return ("closed", "[dim]none yet[/dim]")
    rate = won / total
    return (
        f"[{UP}]{won}W[/{UP}] [{DOWN}]{lost}L[/{DOWN}]",
        f"{gauge(rate, width, UP, DOWN)} {rate * 100:.0f}%",
    )


# ------------------------------------------------------------------ panels


def hero(
    title: str,
    lines: Sequence[RenderableType | None],
    border: str = "white",
    subtitle: str | None = None,
) -> Panel:
    """The headline panel: one glance at where things stand. None lines are
    skipped, so a caller can pass an optional line without branching."""
    body = [line for line in lines if line is not None]
    return Panel(
        Group(*body),
        title=f"[bold]✦ {title}[/bold]" + (f" [dim]· {subtitle}[/dim]" if subtitle else ""),
        title_align="left",
        border_style=border,
        padding=(0, 2),
        expand=False,
    )


def card(title: str, rows: Sequence[Row], color: str, height: int | None = None) -> Panel:
    """A titled box of rows: a dim label, then right-aligned values. Cells never
    wrap, so `height` (rows + 2) is exact and a row of cards lines up."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(
        style="dim",
        no_wrap=True,
        min_width=max((Text.from_markup(r[0]).cell_len for r in rows), default=0),
    )
    for _ in range(max((len(r) for r in rows), default=2) - 1):
        grid.add_column(justify="right", no_wrap=True)
    for row in rows:
        grid.add_row(*row)
    return Panel(
        grid,
        title=f"[bold {color}]{title}[/bold {color}]",
        border_style=color,
        title_align="left",
        padding=(0, 1),
        height=height,
        expand=False,
    )


def cards(*specs: tuple[str, Sequence[Row], str]) -> Columns:
    """(title, rows, colour) cards side by side at one shared height, wrapping
    onto a new row when the terminal is too narrow. Empty cards are dropped."""
    kept = [s for s in specs if s[1]]
    height = max((len(rows) for _, rows, _ in kept), default=0) + 2
    return Columns(
        [card(title, rows, color, height) for title, rows, color in kept], padding=(0, 1)
    )


def dashboard(top: Panel, *specs: tuple[str, Sequence[Row], str]) -> Group:
    """A hero over a row of cards, with a blank line above — the whole look."""
    return Group(Text(""), top, cards(*specs))
