from datetime import date
from decimal import Decimal

from rich.table import Table

from trd.models import BoardRow, EarningsEvent, LotPosition, Position
from trd.services.dashboard import Dashboard, Holding
from trd.services.dca_detail import PlanDetail
from trd.services.dca_projection import BacktestResult, ForecastResult

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


_SPARK_TICKS = "▁▂▃▄▅▆▇█"
_SPARK_WIDTH = 10


def _downsample(values: list[float], width: int) -> list[float]:
    """Average values into `width` buckets so a long series renders as a tight spark."""
    if len(values) <= width:
        return values
    bucket = len(values) / width
    return [
        sum(values[int(i * bucket) : int((i + 1) * bucket)])
        / max(1, int((i + 1) * bucket) - int(i * bucket))
        for i in range(width)
    ]


def sparkline(values: list[float], width: int = _SPARK_WIDTH) -> str:
    """Compact unicode trend, colored by net direction over the window.

    Net direction uses the raw endpoints; the bars use downsampled buckets so a
    30-day series stays a tight ~10-char spark that reads as its own row."""
    if len(values) < 2:
        return ""
    points = _downsample(values, width)
    lo, hi = min(points), max(points)
    span = hi - lo
    chars = [
        _SPARK_TICKS[0 if span == 0 else round((v - lo) / span * (len(_SPARK_TICKS) - 1))]
        for v in points
    ]
    color = "green" if values[-1] >= values[0] else "red"
    return f"[{color}]{''.join(chars)}[/{color}]"


def heat_pct(value: Decimal | None) -> str:
    """P&L% with intensity scaled to magnitude — big moves glow, small ones dim."""
    if value is None:
        return "—"
    mag = abs(value)
    if value >= 0:
        color = "bright_green" if mag >= 100 else "green" if mag >= 15 else "pale_green1"
    else:
        color = "bright_red" if mag >= 50 else "red" if mag >= 15 else "indian_red1"
    return f"[{color}]{'+' if value >= 0 else ''}{value:.1f}%[/{color}]"


def positions_table(
    positions: list[Position],
    title: str,
    sparklines: dict[str, list[float]] | None = None,
) -> Table:
    sparklines = sparklines or {}
    total_value = sum((p.market_value for p in positions if p.market_value is not None), Decimal(0))
    table = Table(title=title, title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("Wt", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("30d", justify="left", no_wrap=True, width=_SPARK_WIDTH)
    table.add_column("Value", justify="right")
    table.add_column("Day Δ%", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L%", justify="right")

    total_cost = Decimal(0)
    total_day = Decimal(0)
    any_value = False
    for p in positions:
        symbol = p.instrument.symbol + (" [dim](stale)[/dim]" if p.price_stale else "")
        weight = (
            f"{p.market_value / total_value * 100:.0f}%"
            if p.market_value is not None and total_value
            else "—"
        )
        table.add_row(
            symbol,
            weight,
            fmt_qty(p.quantity),
            fmt_money(p.price),
            sparkline(sparklines.get(p.instrument.symbol, [])),
            fmt_money(p.market_value),
            fmt_signed_pct(p.day_change_pct),
            fmt_signed(p.unrealized_pl),
            heat_pct(p.unrealized_pl_pct),
        )
        total_cost += p.cost_basis
        if p.market_value is not None:
            any_value = True
        if p.day_change is not None:
            total_day += p.day_change

    if positions and any_value:
        total_pl = total_value - total_cost
        total_pl_pct = total_pl / total_cost * 100 if total_cost else None
        total_day_pct = (
            total_day / (total_value - total_day) * 100 if (total_value - total_day) else None
        )
        table.add_section()
        table.add_row(
            "[bold]Total[/bold]",
            "",
            "",
            "",
            "",
            f"[bold]{fmt_money(total_value)}[/bold]",
            fmt_signed_pct(total_day_pct),
            fmt_signed(total_pl),
            heat_pct(total_pl_pct),
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
    table.add_column("Account", style="dim")
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
            lot.account,
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


def fmt_pct_float(value: float | None) -> str:
    """XIRR/CAGR are floats (signals, not ledger values)."""
    if value is None:
        return "—"
    color = "green" if value >= 0 else "red"
    return f"[{color}]{'+' if value >= 0 else ''}{value * 100:.2f}%[/{color}]"


def dca_summary_table(detail: PlanDetail) -> Table:
    status = detail.status
    plan = detail.plan
    table = Table(title=f"DCA — {plan.account.name}", title_justify="left")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Strategy", plan.strategy_label)
    if plan.note:
        table.add_row("Goal", plan.note)
    day = f"the {plan.day_of_month}th" if plan.day_of_month else "—"
    table.add_row("Schedule", f"{fmt_money(plan.monthly_amount)}/month on {day}")
    table.add_row("State", "active" if plan.active else "[yellow]paused[/yellow]")
    table.add_row("Months invested", str(status.months_invested))
    table.add_row("Total invested", fmt_money(status.invested))
    table.add_row("Current value", fmt_money(status.value))
    table.add_row("P&L", fmt_signed(status.pl))
    table.add_row("P&L %", fmt_signed_pct(status.pl_pct))
    table.add_row("XIRR (annualized)", fmt_pct_float(detail.xirr))
    if status.benchmark_value is not None:
        table.add_row("SPY same dates", fmt_money(status.benchmark_value))
        table.add_row("vs SPY", fmt_signed(status.vs_benchmark))
    return table


def dca_symbols_table(detail: PlanDetail) -> Table:
    table = Table(title="Per symbol", title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("Invested", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("Avg Cost", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L%", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Actual", justify="right")
    table.add_column("Drift", justify="right")
    for stat in detail.symbol_stats:
        drift = stat.drift
        drift_text = "—"
        if drift is not None:
            color = "yellow" if abs(drift) >= 5 else "dim"
            drift_text = f"[{color}]{'+' if drift >= 0 else ''}{drift:.1f}pp[/{color}]"
        table.add_row(
            stat.symbol,
            fmt_money(stat.invested),
            fmt_qty(stat.quantity),
            fmt_money(stat.avg_cost),
            fmt_money(stat.price),
            fmt_money(stat.value),
            fmt_signed(stat.pl),
            fmt_signed_pct(stat.pl_pct),
            f"{stat.target_weight.normalize():f}%" if stat.target_weight is not None else "—",
            f"{stat.actual_weight:.1f}%" if stat.actual_weight is not None else "—",
            drift_text,
        )
    return table


def dca_cadence_table(detail: PlanDetail) -> Table:
    cadence = detail.cadence
    table = Table(title="Cadence", title_justify="left")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Streak", f"{cadence.streak} month(s)")
    missed = cadence.missed
    table.add_row("Missed months", f"[red]{missed}[/red]" if missed else "0")
    table.add_row("Last invested", str(cadence.last_invested) if cadence.last_invested else "—")
    table.add_row("Next due", str(cadence.next_due) if cadence.next_due else "—")
    return table


def dca_history_table(detail: PlanDetail, limit: int | None = None) -> Table:
    table = Table(title=f"Contributions — {detail.plan.account.name}", title_justify="left")
    table.add_column("Date")
    table.add_column("Symbol", style="bold")
    table.add_column("Qty", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Amount", justify="right")
    events = detail.events
    if limit is not None:
        events = events[-limit:]
    for event in events:
        first = True
        for leg in event.legs:
            table.add_row(
                str(event.date) if first else "",
                leg.symbol,
                fmt_qty(leg.quantity),
                fmt_money(leg.price),
                fmt_money(leg.amount),
            )
            first = False
        table.add_row("", "", "", "[dim]total[/dim]", f"[bold]{fmt_money(event.total)}[/bold]")
        table.add_section()
    return table


def _fmt_f(value: float | None) -> str:
    return f"{value:,.0f}" if value is not None else "—"


def forecast_table(result: ForecastResult) -> Table:
    table = Table(
        title=f"Forecast — ${result.monthly:,.0f}/month for {result.months // 12} years",
        title_justify="left",
    )
    table.add_column("Year", justify="right")
    table.add_column("Contributed", justify="right")
    table.add_column("Expected", justify="right")
    table.add_column("Bad case (p10)", justify="right")
    table.add_column("Median (p50)", justify="right")
    table.add_column("Good case (p90)", justify="right")
    for band in result.years:
        table.add_row(
            str(band.year),
            _fmt_f(band.contributed),
            _fmt_f(band.deterministic),
            f"[red]{_fmt_f(band.p10)}[/red]",
            _fmt_f(band.p50),
            f"[green]{_fmt_f(band.p90)}[/green]",
        )
    return table


def backtest_table(result: BacktestResult, account: str) -> Table:
    table = Table(
        title=f"Backtest — {account}, {result.start} to {result.end}", title_justify="left"
    )
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Months invested", str(result.months))
    if result.skipped_months:
        table.add_row("Skipped months", f"[yellow]{result.skipped_months}[/yellow]")
    table.add_row("Invested", _fmt_f(result.invested))
    table.add_row("Ending value", _fmt_f(result.value))
    pl_pct = f" ({result.pl_pct:+.1f}%)" if result.pl_pct is not None else ""
    table.add_row("P&L", f"{result.pl:+,.0f}{pl_pct}")
    table.add_row("XIRR (annualized)", fmt_pct_float(result.xirr))
    table.add_row("SPY same cashflows", _fmt_f(result.spy_value))
    table.add_row("SPY XIRR", fmt_pct_float(result.spy_xirr))
    color = "green" if result.vs_spy >= 0 else "red"
    table.add_row("vs SPY", f"[{color}]{result.vs_spy:+,.0f}[/{color}]")
    return table


def _money0(value):
    return f"{value:,.0f}" if value is not None else "—"


def dashboard_card(dash: Dashboard) -> Table:
    """The compact 'five metrics that matter' home view."""
    table = Table(title="Portfolio", title_justify="left", show_header=False)
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Portfolio value", f"[bold]{fmt_money(dash.value)}[/bold]")
    table.add_row("Total return", fmt_signed_pct(dash.total_return_pct))
    table.add_row("XIRR (annualized)", fmt_pct_float(dash.xirr))
    if dash.alpha is not None:
        table.add_row("vs S&P 500", fmt_signed_pct(dash.alpha))
    table.add_section()
    table.add_row("Amount invested", fmt_money(dash.invested))
    table.add_row("Investment gains", fmt_signed(dash.gains))
    table.add_section()
    today = f"{fmt_signed(dash.today_change)} ({fmt_signed_pct(dash.today_change_pct)})"
    table.add_row("Today's change", today)
    if dash.spy_today_pct is not None:
        table.add_row("S&P 500 today", fmt_signed_pct(dash.spy_today_pct))
    table.add_section()
    top = dash.top_holding
    if top is not None:
        warn = "  [yellow]⚠ concentration[/yellow]" if dash.concentration_warning else ""
        table.add_row("Top holding", f"{top.symbol} ({top.weight:.1f}%){warn}")
    if dash.winners:
        w = dash.winners[0]
        table.add_row("Largest winner", f"{w.symbol} ({fmt_signed_pct(w.pl_pct)})")
    if dash.losers:
        x = dash.losers[0]
        table.add_row("Largest loser", f"{x.symbol} ({fmt_signed_pct(x.pl_pct)})")
    return table


def dashboard_allocation_table(dash: Dashboard) -> Table:
    table = Table(title="Allocation", title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("Value", justify="right")
    table.add_column("Weight", justify="right")
    for h in dash.holdings:
        table.add_row(h.symbol, fmt_money(h.value), f"{h.weight:.1f}%")
    table.add_section()
    table.add_row("[dim]Top 5[/dim]", "", f"[dim]{dash.top5_weight:.1f}% of portfolio[/dim]")
    return table


def _movers_table(title: str, holdings: list[Holding]) -> Table:
    table = Table(title=title, title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("Return", justify="right")
    table.add_column("P&L", justify="right")
    for h in holdings:
        table.add_row(h.symbol, fmt_signed_pct(h.pl_pct), fmt_signed(h.pl))
    return table


def dashboard_movers(dash: Dashboard) -> tuple[Table, Table]:
    return (
        _movers_table("Biggest winners", dash.winners),
        _movers_table("Biggest losers", dash.losers),
    )
