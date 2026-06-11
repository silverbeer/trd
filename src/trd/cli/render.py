from datetime import date
from decimal import Decimal

from rich.table import Table

from trd.models import BoardRow, EarningsEvent, LotPosition, Position

MONEY = "{:,.2f}"


def fmt_money(value: Decimal | None) -> str:
    return MONEY.format(value) if value is not None else "—"


def fmt_qty(value: Decimal) -> str:
    text = f"{value.normalize():f}"
    return text


def fmt_signed(value: Decimal | None) -> str:
    if value is None:
        return "—"
    color = "green" if value >= 0 else "red"
    return f"[{color}]{'+' if value >= 0 else ''}{MONEY.format(value)}[/{color}]"


def fmt_signed_pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    color = "green" if value >= 0 else "red"
    return f"[{color}]{'+' if value >= 0 else ''}{value:.2f}%[/{color}]"


def positions_table(positions: list[Position], title: str) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Day Δ", justify="right")
    table.add_column("Day Δ%", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L%", justify="right")

    total_value = Decimal(0)
    total_cost = Decimal(0)
    total_day = Decimal(0)
    any_value = False
    for p in positions:
        symbol = p.instrument.symbol + (" [dim](stale)[/dim]" if p.price_stale else "")
        table.add_row(
            symbol,
            fmt_qty(p.quantity),
            fmt_money(p.avg_cost),
            fmt_money(p.price),
            fmt_money(p.market_value),
            fmt_signed(p.day_change),
            fmt_signed_pct(p.day_change_pct),
            fmt_signed(p.unrealized_pl),
            fmt_signed_pct(p.unrealized_pl_pct),
        )
        total_cost += p.cost_basis
        if p.market_value is not None:
            total_value += p.market_value
            any_value = True
        if p.day_change is not None:
            total_day += p.day_change

    if positions and any_value:
        total_pl = total_value - total_cost
        total_pl_pct = total_pl / total_cost * 100 if total_cost else None
        table.add_section()
        table.add_row(
            "[bold]Total[/bold]",
            "",
            "",
            "",
            f"[bold]{fmt_money(total_value)}[/bold]",
            fmt_signed(total_day),
            "",
            fmt_signed(total_pl),
            fmt_signed_pct(total_pl_pct),
        )
    return table


def _fmt_year_range(pct: Decimal | None) -> str:
    if pct is None:
        return "—"
    color = "green" if pct >= 50 else "yellow" if pct >= 20 else "red"
    return f"[{color}]{pct:.0f}%[/{color}]"


def _fmt_volume_ratio(ratio: Decimal | None) -> str:
    if ratio is None:
        return "—"
    text = f"{ratio:.1f}x"
    return f"[bold yellow]{text}[/bold yellow]" if ratio >= Decimal("1.5") else text


def _fmt_earnings_date(when: date | None, today: date) -> str:
    if when is None:
        return "—"
    days = (when - today).days
    text = f"{when} ({days}d)"
    return f"[bold red]{text}[/bold red]" if days <= 7 else text


def board_table(rows: list[BoardRow], title: str, show_list_column: bool) -> Table:
    today = date.today()
    table = Table(title=title, title_justify="left")
    if show_list_column:
        table.add_column("List", style="dim")
    table.add_column("Symbol", style="bold")
    table.add_column("Price", justify="right")
    table.add_column("Day Δ%", justify="right")
    table.add_column("52w Pos", justify="right")
    table.add_column("Vol/Avg", justify="right")
    table.add_column("Earnings", justify="right")
    for row in rows:
        quote = row.quote
        symbol = row.instrument.symbol + (" [dim](stale)[/dim]" if row.price_stale else "")
        cells = [
            symbol,
            fmt_money(quote.price if quote else None),
            fmt_signed_pct(quote.day_change_pct if quote else None),
            _fmt_year_range(quote.year_range_pct if quote else None),
            _fmt_volume_ratio(quote.volume_ratio if quote else None),
            _fmt_earnings_date(row.next_earnings, today),
        ]
        if show_list_column:
            cells.insert(0, row.watchlist)
        table.add_row(*cells)
    return table


def lots_table(lots: list[LotPosition], title: str) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("Bought", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("Paid/sh", justify="right")
    table.add_column("Total Cost", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("Gain", justify="right")
    table.add_column("Gain%", justify="right")

    total_cost = Decimal(0)
    total_value = Decimal(0)
    any_value = False
    last_symbol = None
    for lot in lots:
        symbol = lot.instrument.symbol
        display = symbol if symbol != last_symbol else ""
        last_symbol = symbol
        if lot.price_stale and display:
            display += " [dim](stale)[/dim]"
        table.add_row(
            display,
            str(lot.bought_at.date()),
            fmt_qty(lot.quantity),
            fmt_money(lot.price_paid),
            fmt_money(lot.cost),
            fmt_money(lot.price),
            fmt_money(lot.value),
            fmt_signed(lot.gain),
            fmt_signed_pct(lot.gain_pct),
        )
        total_cost += lot.cost
        if lot.value is not None:
            total_value += lot.value
            any_value = True

    if lots and any_value:
        total_gain = total_value - total_cost
        total_gain_pct = total_gain / total_cost * 100 if total_cost else None
        table.add_section()
        table.add_row(
            "[bold]Total[/bold]",
            "",
            "",
            "",
            f"[bold]{fmt_money(total_cost)}[/bold]",
            "",
            f"[bold]{fmt_money(total_value)}[/bold]",
            fmt_signed(total_gain),
            fmt_signed_pct(total_gain_pct),
        )
    return table


def indicator_panel(rows: list, symbol: str) -> Table:
    """Learning-mode panel: one row per followed indicator with a plain-English read."""
    table = Table(title=f"Indicators — {symbol}", title_justify="left")
    table.add_column("Indicator", style="bold")
    table.add_column("Params", style="dim")
    table.add_column("Value", justify="right")
    table.add_column("Reading")
    category_styles = {
        "trend": "cyan",
        "momentum": "magenta",
        "volatility": "yellow",
        "volume": "green",
    }
    last_category = None
    for row in rows:
        if row.category != last_category:
            style = category_styles.get(row.category, "white")
            table.add_section()
            table.add_row(f"[{style}]{row.category.upper()}[/{style}]", "", "", "")
            last_category = row.category
        params = ", ".join(f"{k}={v}" for k, v in row.config.params.items()) or "—"
        if not row.values:
            value_text = "—"
        elif len(row.values) == 1:
            v = next(iter(row.values.values()))
            value_text = f"{v:,.2f}" if v is not None else "—"
        else:
            value_text = " / ".join(
                f"{v:,.2f}" if v is not None else "—" for v in row.values.values()
            )
        table.add_row(f"  {row.name}", params, value_text, row.reading)
    return table


def earnings_table(events: list[EarningsEvent], days: int) -> Table:
    today = date.today()
    table = Table(title=f"Earnings — next {days} days", title_justify="left")
    table.add_column("Date")
    table.add_column("In", justify="right")
    table.add_column("Symbol", style="bold")
    table.add_column("Name")
    table.add_column("EPS Est", justify="right")
    for event in events:
        days_out = (event.date - today).days
        table.add_row(
            str(event.date),
            f"{days_out}d",
            event.instrument.symbol,
            event.instrument.name or "—",
            f"{event.eps_estimate:.2f}" if event.eps_estimate is not None else "—",
        )
    return table
