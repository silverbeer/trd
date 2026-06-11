from decimal import Decimal

from rich.table import Table

from trd.models import Position

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
