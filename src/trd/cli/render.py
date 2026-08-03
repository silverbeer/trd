from datetime import date, datetime
from decimal import Decimal

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trd.engine import EXIT_RULES
from trd.engine import REGISTRY as STRATEGIES
from trd.models import (
    BoardRow,
    EarningsEvent,
    EngineRun,
    EngineStatus,
    ExitCheckRow,
    ExitStatus,
    LotPosition,
    Position,
    PositionRow,
    PositionStatus,
    Side,
    SignalRow,
    StrategyStat,
    TradeExplanation,
)
from trd.repos import PrepSnapshotRow
from trd.services.backtest import BacktestResult as EngineBacktestResult
from trd.services.dashboard import Dashboard, Holding
from trd.services.dca_detail import PlanDetail
from trd.services.dca_projection import BacktestResult, ForecastResult
from trd.services.engine import ScanResult
from trd.services.equity_curve import EquityCurve
from trd.services.history import HistoryResult
from trd.services.movers import MoverRow
from trd.services.plan import PlanStatus
from trd.services.sunday_prep import SundayPrepBriefing

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


def trend_change(values: list[float]) -> str:
    """30-day change as a colored percent with a direction arrow. Font-proof —
    unlike block-glyph sparklines, this renders cleanly in any terminal."""
    if len(values) < 2 or values[0] == 0:
        return "—"
    pct = (values[-1] - values[0]) / values[0] * 100
    arrow = "↑" if pct > 0.05 else "↓" if pct < -0.05 else "→"
    color = "green" if pct > 0 else "red" if pct < 0 else "dim"
    return f"[{color}]{arrow}{abs(pct):.0f}%[/{color}]"


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
    table.add_column("30d", justify="right")
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
            trend_change(sparklines.get(p.instrument.symbol, [])),
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


_EXIT_STATUS_LABEL = {
    ExitStatus.STOP_HIT: "[bold red]⚠ STOP HIT[/bold red]",
    ExitStatus.TARGET_HIT: "[bold green]✓ TARGET[/bold green]",
    ExitStatus.OK: "[dim]holding[/dim]",
    ExitStatus.NO_PRICE: "[yellow]no close — sync[/yellow]",
}


def exit_table(rows: list[ExitCheckRow], title: str, show_account: bool = True) -> Table:
    """Exit triggers vs the latest close: stop, cushion, target, room, and status."""
    table = Table(title=title, title_justify="left")
    if show_account:
        table.add_column("Account", style="dim")
    table.add_column("Symbol", style="bold")
    table.add_column("Last Close", justify="right")
    table.add_column("Stop", justify="right")
    table.add_column("Cushion", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Room", justify="right")
    table.add_column("Status")
    table.add_column("Note", style="dim")
    for row in rows:
        cells = [
            row.instrument.symbol,
            fmt_money(row.last_close),
            fmt_money(row.stop_price),
            fmt_signed_pct(row.stop_cushion_pct),
            fmt_money(row.target_price),
            fmt_signed_pct(row.target_upside_pct),
            _EXIT_STATUS_LABEL[row.status],
            row.note or "",
        ]
        if show_account:
            cells.insert(0, row.account)
        table.add_row(*cells)
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


_TYPE_LABEL = {"etf": "ETF", "stock": "Stock", "crypto": "Crypto"}


def board_table(rows: list[BoardRow], title: str, show_list_column: bool) -> Table:
    today = date.today()
    table = Table(title=title, title_justify="left")
    if show_list_column:
        table.add_column("List", style="dim")
    table.add_column("Symbol", style="bold")
    table.add_column("Own", justify="center")
    table.add_column("Type", style="dim")
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
            "[green]●[/green]" if row.owned else "",
            _TYPE_LABEL.get(row.instrument.type.value, row.instrument.type.value),
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


# --- Equity curve -----------------------------------------------------------


def _fmt_pct_signed(value: float | None) -> str:
    if value is None:
        return "—"
    color = "green" if value >= 0 else "red"
    return f"[{color}]{'+' if value >= 0 else ''}{value:.2f}%[/{color}]"


def _downsample(values: list[float], width: int) -> list[float]:
    n = len(values)
    if n <= width:
        return values
    out: list[float] = []
    for i in range(width):
        s = int(i * n / width)
        e = max(int((i + 1) * n / width), s + 1)
        chunk = values[s:e]
        out.append(sum(chunk) / len(chunk))
    return out


def line_chart(
    values: list[float],
    start_label: str,
    end_label: str,
    width: int = 64,
    height: int = 12,
    baseline: float | None = None,
) -> Text:
    """A connected line chart of a value series.

    Box-drawing only, matching `trend_change`'s refusal to use block glyphs —
    they render inconsistently across fonts, and the Rich tables already depend
    on box-drawing so this adds no new risk.

    Two things the earlier scatter left invisible and this marks explicitly:
    where the series started, so above and below water is a glance rather than
    an arithmetic problem, and the deepest trough, which is the number that
    decides whether a curve was survivable rather than merely profitable.
    """
    if not values:
        return Text("")
    sampled = _downsample(values, width)
    lo, hi = min(sampled), max(sampled)
    if baseline is not None:
        lo, hi = min(lo, baseline), max(hi, baseline)
    span = (hi - lo) or 1.0

    def level(v: float) -> int:
        return round((v - lo) / span * (height - 1))

    grid = [[" "] * len(sampled) for _ in range(height)]
    if baseline is not None:
        row = height - 1 - level(baseline)
        grid[row] = ["┄"] * len(sampled)

    previous: int | None = None
    for col, v in enumerate(sampled):
        current = level(v)
        # Join to the previous point so the eye follows a line rather than
        # assembling one out of dots.
        if previous is not None:
            for between in range(min(previous, current) + 1, max(previous, current)):
                grid[height - 1 - between][col] = "│"
        grid[height - 1 - current][col] = "─"
        previous = current

    # Computed on the FULL series, then mapped to a column: measuring the
    # downsampled copy would smooth the true trough away and print a different
    # drawdown from the one the summary reports, for the same curve.
    peak, worst, trough_at = values[0], 0.0, None
    for idx, v in enumerate(values):
        peak = max(peak, v)
        drop = (v - peak) / peak * 100 if peak else 0.0
        if drop < worst:
            worst, trough_at = drop, idx
    trough_col = (
        round(trough_at / max(len(values) - 1, 1) * (len(sampled) - 1))
        if trough_at is not None
        else None
    )
    if trough_col is not None:
        grid[height - 1 - level(sampled[trough_col])][trough_col] = "▼"

    rising = values[-1] >= values[0]
    colour = "green" if rising else "red"
    lines: list[str] = []
    for r, row in enumerate(grid):
        if r == 0:
            axis = f"{hi:>12,.0f} "
        elif r == height - 1:
            axis = f"{lo:>12,.0f} "
        else:
            axis = " " * 13
        painted = "".join(row).replace("┄", f"[/{colour}][dim]┄[/dim][{colour}]")
        painted = painted.replace("▼", f"[/{colour}][bold red]▼[/bold red][{colour}]")
        lines.append(f"[dim]{axis}[/dim][{colour}]{painted}[/{colour}]")
    pad = max(len(sampled) - len(start_label) - len(end_label), 1)
    lines.append(f"{' ' * 13}[dim]{start_label}{' ' * pad}{end_label}[/dim]")
    if trough_col is not None and worst < -0.05:
        lines.append(
            f"{' ' * 13}[dim]▼ deepest drawdown {worst:.1f}%"
            + ("  ┄ starting value" if baseline is not None else "")
            + "[/dim]"
        )
    return Text.from_markup("\n".join(lines))


def equity_chart(curve: EquityCurve, width: int = 64, height: int = 12) -> Text:
    """Portfolio value over time, against where it started."""
    values = [float(p.value) for p in curve.points]
    return line_chart(
        values,
        str(curve.start_date),
        str(curve.end_date),
        width=width,
        height=height,
        baseline=values[0] if values else None,
    )


def equity_summary_table(curve: EquityCurve) -> Table:
    table = Table(show_header=False, title=None, box=None, padding=(0, 2, 0, 0))
    table.add_column("k", style="dim")
    table.add_column("v")
    days = (curve.end_date - curve.start_date).days
    table.add_row("Period", f"{curve.start_date} → {curve.end_date}  ({days}d)")
    table.add_row(
        "Value", f"{fmt_money(curve.start_value)} → [bold]{fmt_money(curve.end_value)}[/bold]"
    )
    table.add_row("Period return", _fmt_pct_signed(curve.period_return_pct))
    table.add_row("Unrealized P&L", _fmt_pct_signed(curve.pl_pct))
    table.add_row("XIRR", _fmt_pct_signed(curve.xirr * 100 if curve.xirr is not None else None))
    table.add_row("Max drawdown", f"[red]{curve.max_drawdown_pct:.2f}%[/red]")
    table.add_row("Peak value", fmt_money(curve.peak_value))
    return table


def equity_curve_renderables(curve: EquityCurve) -> list[RenderableType]:
    out: list[RenderableType] = [
        Text.from_markup(f"[bold]Equity curve[/bold] — {curve.account_label}"),
        equity_chart(curve),
        Text(""),
        equity_summary_table(curve),
    ]
    if curve.unpriced:
        out.append(
            Text.from_markup(
                f"[yellow]No price history for: {', '.join(curve.unpriced)} — "
                f"run 'trd sync --years N'. Excluded from the curve.[/yellow]"
            )
        )
    return out


# --- Sunday Prep ------------------------------------------------------------


def _fmt_level(value: Decimal | None) -> str:
    return f"{value:,.2f}" if value is not None else "—"


def _num_section(n: int, title: str) -> Text:
    return Text.from_markup(f"[bold cyan]{n}. {title}[/bold cyan]")


def sunday_prep_renderables(b: SundayPrepBriefing) -> list[RenderableType]:
    """The full Sunday Prep briefing as a sequence of Rich renderables."""
    out: list[RenderableType] = []

    week = f"{b.week_start:%b %d} - {b.week_end:%b %d, %Y}"
    header = Text.from_markup(
        f"[bold]TRD Sunday Prep[/bold]  [dim]· week of {week}[/dim]\n\n{b.tone}"
    )
    out.append(Panel(header, border_style="cyan"))

    # 1. Futures
    ft = Table(title=None, expand=False)
    ft.add_column("Contract")
    ft.add_column("Last", justify="right")
    ft.add_column("Change", justify="right")
    for f in b.futures:
        change = fmt_signed_pct(f.change_pct)
        if f.unusual:
            change += " [yellow]⚠[/yellow]"
        ft.add_row(f.label, _fmt_level(f.price), change)
    out.append(Group(_num_section(1, "Futures Snapshot"), ft))

    # 1b. Commodities (oil + gold)
    if b.commodities:
        ct = Table(title=None, expand=False)
        ct.add_column("Commodity")
        ct.add_column("Last", justify="right")
        ct.add_column("Change", justify="right")
        for c in b.commodities:
            change = fmt_signed_pct(c.change_pct)
            if c.unusual:
                change += " [yellow]⚠[/yellow]"
            ct.add_row(c.label, _fmt_level(c.price), change)
        out.append(Group(Text.from_markup("[bold cyan]Commodities[/bold cyan]"), ct))

    # 2. Economic events
    if b.econ_events:
        et = Table()
        et.add_column("Day")
        et.add_column("Time")
        et.add_column("Event", style="bold")
        et.add_column("Why it matters")
        for e in b.econ_events:
            et.add_row(f"{e.day} {e.date:%m/%d}", e.time_et or "—", e.name, e.why)
        out.append(Group(_num_section(2, "Week's Major Events"), et))
    else:
        out.append(
            Group(
                _num_section(2, "Week's Major Events"), Text("  Quiet macro calendar.", style="dim")
            )
        )

    # 3. Earnings
    if b.earnings:
        ent = Table()
        ent.add_column("Day")
        ent.add_column("Symbol", style="bold")
        ent.add_column("Company")
        ent.add_column("Session")
        ent.add_column("Why it matters")
        for e in b.earnings:
            ent.add_row(f"{e.day} {e.date:%m/%d}", e.symbol, e.name, e.timing, e.why)
        out.append(Group(_num_section(3, "Earnings Calendar"), ent))
    else:
        out.append(
            Group(
                _num_section(3, "Earnings Calendar"),
                Text("  No tracked-universe names report this week.", style="dim"),
            )
        )

    # 4. Leadership
    lt = Table()
    lt.add_column("")
    lt.add_column("Sector", style="bold")
    lt.add_column("Week", justify="right")
    for mv in b.sector_leaders:
        lt.add_row("[green]▲[/green]", f"{mv.name} ({mv.symbol})", fmt_signed_pct(mv.week_pct))
    if b.sector_leaders and b.sector_laggards:
        lt.add_section()
    for mv in b.sector_laggards:
        lt.add_row("[red]▼[/red]", f"{mv.name} ({mv.symbol})", fmt_signed_pct(mv.week_pct))
    out.append(Group(_num_section(4, "Market Leadership"), lt))

    # 5. Volatility
    vix = b.volatility.vix
    vix_txt = f"[bold]{vix}[/bold]" if vix is not None else "—"
    out.append(
        Group(
            _num_section(5, "Volatility Check"),
            Text.from_markup(
                f"  VIX {vix_txt} — {b.volatility.band}\n  [dim]{b.volatility.note}[/dim]"
            ),
        )
    )

    # 6. Key levels
    kt = Table()
    kt.add_column("ETF", style="bold")
    kt.add_column("Price", justify="right")
    kt.add_column("50d", justify="right")
    kt.add_column("200d", justify="right")
    kt.add_column("52w Hi", justify="right")
    kt.add_column("52w Lo", justify="right")
    kt.add_column("ATR", justify="right")
    kt.add_column("Read")
    for lvl in b.key_levels:
        kt.add_row(
            lvl.symbol,
            _fmt_level(lvl.price),
            _fmt_level(lvl.sma50),
            _fmt_level(lvl.sma200),
            _fmt_level(lvl.high52),
            _fmt_level(lvl.low52),
            _fmt_level(lvl.atr),
            lvl.note,
        )
    out.append(Group(_num_section(6, "Key Levels"), kt))

    # 7. Themes
    theme_lines = "\n".join(f"  • [bold]{t.title}[/bold] — {t.why}" for t in b.themes)
    out.append(Group(_num_section(7, "Themes to Watch"), Text.from_markup(theme_lines)))

    # 8. Watchlist
    wt = Table()
    wt.add_column("Symbol", style="bold")
    wt.add_column("Rationale")
    wt.add_column("Catalyst")
    for w in b.watchlist:
        wt.add_row(w.symbol, w.rationale, w.catalyst)
    out.append(
        Group(
            _num_section(8, "Build a Watchlist"),
            wt,
            Text(
                "  Not recommendations — candidates to study before the open.", style="dim italic"
            ),
        )
    )

    # 9. Risks
    risk_lines = "\n".join(f"  • {r}" for r in b.risks)
    out.append(Group(_num_section(9, "Risk Assessment"), Text(risk_lines)))

    # 10. Mindset
    out.append(
        Panel(
            Text.from_markup(f"{b.mindset}\n\n[bold cyan]{b.prompt_question}[/bold cyan]"),
            title="10. Weekly Mindset",
            title_align="left",
            border_style="cyan",
        )
    )
    return out


def _md_pct(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{'+' if value >= 0 else ''}{value:.2f}%"


def sunday_prep_markdown(b: SundayPrepBriefing) -> str:
    """GitHub-flavored markdown of the briefing — the snapshot artifact a Claude
    session (or any reader) can narrate over."""
    lines: list[str] = []
    lines.append("## TRD Sunday Prep")
    lines.append(f"*Week of {b.week_start:%b %d} - {b.week_end:%b %d, %Y}*")
    lines.append("")
    lines.append(b.tone)
    lines.append("")

    lines.append("### 1. Futures Snapshot")
    lines.append("| Contract | Last | Change |")
    lines.append("|---|--:|--:|")
    for f in b.futures:
        flag = " ⚠️" if f.unusual else ""
        lines.append(f"| {f.label} | {_fmt_level(f.price)} | {_md_pct(f.change_pct)}{flag} |")
    lines.append("")

    if b.commodities:
        lines.append("**Commodities**")
        lines.append("| Commodity | Last | Change |")
        lines.append("|---|--:|--:|")
        for c in b.commodities:
            flag = " ⚠️" if c.unusual else ""
            lines.append(f"| {c.label} | {_fmt_level(c.price)} | {_md_pct(c.change_pct)}{flag} |")
        lines.append("")

    lines.append("### 2. Week's Major Events")
    if b.econ_events:
        lines.append("| Day | Time | Event | Why |")
        lines.append("|---|---|---|---|")
        for e in b.econ_events:
            lines.append(f"| {e.day} {e.date:%m/%d} | {e.time_et or '—'} | {e.name} | {e.why} |")
    else:
        lines.append("Quiet macro calendar.")
    lines.append("")

    lines.append("### 3. Earnings Calendar")
    if b.earnings:
        lines.append("| Day | Symbol | Company | Session | Why |")
        lines.append("|---|---|---|---|---|")
        for e in b.earnings:
            lines.append(
                f"| {e.day} {e.date:%m/%d} | {e.symbol} | {e.name} | {e.timing} | {e.why} |"
            )
    else:
        lines.append("No tracked-universe names report this week.")
    lines.append("")

    lines.append("### 4. Market Leadership")
    for mv in b.sector_leaders:
        lines.append(f"- ▲ **{mv.name}** ({mv.symbol}) {_md_pct(mv.week_pct)}")
    for mv in b.sector_laggards:
        lines.append(f"- ▼ {mv.name} ({mv.symbol}) {_md_pct(mv.week_pct)}")
    lines.append("")

    lines.append("### 5. Volatility Check")
    vix = b.volatility.vix
    lines.append(
        f"VIX **{vix if vix is not None else '—'}** — {b.volatility.band}. {b.volatility.note}"
    )
    lines.append("")

    lines.append("### 6. Key Levels")
    lines.append("| ETF | Price | 50d | 200d | 52w Hi | 52w Lo | ATR | Read |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|---|")
    for lvl in b.key_levels:
        lines.append(
            f"| {lvl.symbol} | {_fmt_level(lvl.price)} | {_fmt_level(lvl.sma50)} | "
            f"{_fmt_level(lvl.sma200)} | {_fmt_level(lvl.high52)} | {_fmt_level(lvl.low52)} | "
            f"{_fmt_level(lvl.atr)} | {lvl.note} |"
        )
    lines.append("")

    lines.append("### 7. Themes to Watch")
    for t in b.themes:
        lines.append(f"- **{t.title}** — {t.why}")
    lines.append("")

    lines.append("### 8. Build a Watchlist")
    lines.append("| Symbol | Rationale | Catalyst |")
    lines.append("|---|---|---|")
    for w in b.watchlist:
        lines.append(f"| {w.symbol} | {w.rationale} | {w.catalyst} |")
    lines.append("")
    lines.append("*Not recommendations — candidates to study before the open.*")
    lines.append("")

    lines.append("### 9. Risk Assessment")
    for r in b.risks:
        lines.append(f"- {r}")
    lines.append("")

    lines.append("### 10. Weekly Mindset")
    lines.append(b.mindset)
    lines.append("")
    lines.append(f"**{b.prompt_question}**")
    lines.append("")
    return "\n".join(lines)


def _md_sector(symbol: str | None, pct: float | None) -> str:
    if symbol is None:
        return "—"
    return f"{symbol} {pct:+.2f}%" if pct is not None else symbol


def prep_history_table(rows: list[PrepSnapshotRow]) -> Table:
    """Saved Sunday Prep briefings as a week-over-week trend."""
    table = Table(title="Sunday Prep history", title_justify="left")
    table.add_column("Date")
    table.add_column("Week of")
    table.add_column("VIX", justify="right")
    table.add_column("Regime")
    table.add_column("Breadth", justify="right")
    table.add_column("Top sector")
    table.add_column("Worst sector")
    table.add_column("FOMC", justify="center")
    table.add_column("Earn", justify="right")
    for r in rows:
        vix = f"{r.vix:.2f}" if r.vix is not None else "—"
        breadth = f"{r.avg_futures_pct:+.2f}%" if r.avg_futures_pct is not None else "—"
        table.add_row(
            str(r.snapshot_date),
            f"{r.week_start:%b %d}",
            vix,
            r.vix_band.split(" — ")[0],
            breadth,
            _md_sector(r.top_sector, r.top_sector_pct),
            _md_sector(r.worst_sector, r.worst_sector_pct),
            "●" if r.fomc_week else "",
            str(r.earnings_count),
        )
    return table


def equity_daily_table(curve: EquityCurve, limit: int = 30) -> Table:
    """Day-over-day flow-adjusted P&L (contributions stripped out), newest first."""
    table = Table(title=f"Daily P&L — {curve.account_label}", title_justify="left")
    table.add_column("Date")
    table.add_column("Value", justify="right")
    table.add_column("Day +/-", justify="right")
    table.add_column("Day %", justify="right")
    # Skip the first point (no prior day to diff against); show the most recent `limit`.
    rows = [p for p in curve.points[1:]][-limit:]
    for p in reversed(rows):
        table.add_row(
            str(p.date),
            fmt_money(p.value),
            fmt_signed(p.day_pnl),
            _fmt_pct_signed(p.day_pnl_pct),
        )
    return table


def movers_table(rows: list[MoverRow], title: str) -> Table:
    """Owned + watched names ranked by move. Owned carry P&L; watch-only show price/day."""
    table = Table(title=title, title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("", style="dim")  # own / watch tag
    table.add_column("Price", justify="right")
    table.add_column("Day Δ%", justify="right")
    table.add_column("Day $", justify="right")
    table.add_column("30d", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L%", justify="right")
    for r in rows:
        if r.owned and r.watched:
            tag = "own+watch"
        elif r.owned:
            tag = "own"
        else:
            tag = "watch"
        table.add_row(
            r.symbol,
            tag,
            fmt_money(r.price),
            fmt_signed_pct(r.day_pct),
            fmt_signed(r.day_change),
            trend_change(r.spark),
            fmt_money(r.value),
            fmt_signed(r.pl),
            heat_pct(r.pl_pct),
        )
    return table


def plans_pnl_table(statuses: list[PlanStatus]) -> Table:
    """Every contribution plan with its value, P&L, and lead/lag vs SPY — the
    at-a-glance 'which plans are winning' board."""
    table = Table(title="DCA plans — P&L", title_justify="left")
    table.add_column("Account", style="bold")
    table.add_column("Type")
    table.add_column("Strategy")
    table.add_column("Mo", justify="right")
    table.add_column("Invested", justify="right")
    table.add_column("Value", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("P&L%", justify="right")
    table.add_column("vs SPY", justify="right")
    for s in statuses:
        table.add_row(
            s.plan.account.name,
            "paper" if s.plan.is_paper else "real",
            s.plan.strategy_label,
            str(s.months_invested),
            fmt_money(s.invested),
            fmt_money(s.value),
            fmt_signed(s.pl),
            heat_pct(s.pl_pct),
            fmt_signed(s.vs_benchmark),
        )
    return table


def _score_bar(score: float) -> str:
    """Confidence as five blocks. Only meaningful *between* same-bar candidates."""
    filled = max(1, min(5, round(score * 5)))
    color = "green" if score >= 0.6 else "yellow" if score >= 0.35 else "dim"
    return f"[{color}]{'█' * filled}{'·' * (5 - filled)}[/{color}] {score:.2f}"


def engine_scan_renderables(result: ScanResult) -> list[RenderableType]:
    """One scan pass, narrated: what closed, what opened, what merely fired."""
    out: list[RenderableType] = []
    stamp = result.at.strftime("%Y-%m-%d %H:%M:%S")
    mode = "paper fills" if result.paper else "signals only (no fills)"
    header = (
        f"[bold]engine scan[/bold]  {stamp}  ·  {result.scanned} symbols  ·  {mode}\n"
        f"open {result.open_positions}  ·  room for {result.capacity} more"
    )
    out.append(Panel(header, expand=False, border_style="cyan"))

    if result.closed:
        table = Table(title="Closed", title_justify="left")
        table.add_column("Symbol", style="bold")
        table.add_column("Strategy")
        table.add_column("Rule")
        table.add_column("Qty", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("P&L", justify="right")
        table.add_column("R", justify="right")
        table.add_column("Why", style="dim")
        for fill in result.closed:
            table.add_row(
                fill.symbol,
                fill.strategy,
                fill.rule or "—",
                fmt_qty(fill.quantity),
                fmt_money(fill.price),
                fmt_signed(fill.pnl),
                f"{fill.r_multiple:+.2f}R" if fill.r_multiple is not None else "—",
                fill.reason,
            )
        out.append(table)

    if result.opened:
        table = Table(title="Opened", title_justify="left")
        table.add_column("Symbol", style="bold")
        table.add_column("Strategy")
        table.add_column("Qty", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Why", style="dim")
        for fill in result.opened:
            table.add_row(
                fill.symbol,
                fill.strategy,
                fmt_qty(fill.quantity),
                fmt_money(fill.price),
                fill.reason,
            )
        out.append(table)

    unacted = [s for s in result.signals if not s.acted]
    if unacted:
        table = Table(title="Signals not taken", title_justify="left")
        table.add_column("Symbol", style="bold")
        table.add_column("Strategy")
        table.add_column("Score", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Why", style="dim")
        for signal in unacted:
            table.add_row(
                signal.symbol,
                signal.strategy,
                _score_bar(signal.score),
                fmt_money(signal.price),
                signal.reason,
            )
        out.append(table)

    if result.skipped:
        out.append(
            Panel(
                "\n".join(f"· {line}" for line in result.skipped),
                title="Skipped",
                title_align="left",
                border_style="yellow",
                expand=False,
            )
        )

    if result.quiet:
        out.append(
            Text(
                "Nothing fired. A quiet scan is the normal case — "
                "most days no rule's conditions line up.",
                style="dim",
            )
        )
    return out


def engine_signals_table(rows: list[SignalRow]) -> Table:
    """Signal history: every entry the rules saw, taken or passed over."""
    table = Table(title="Engine signals", title_justify="left")
    table.add_column("Fired", style="dim")
    table.add_column("Symbol", style="bold")
    table.add_column("Strategy")
    table.add_column("Score", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Taken", justify="center")
    table.add_column("Why", style="dim")
    for row in rows:
        table.add_row(
            row.signal.fired_at.strftime("%m-%d %H:%M"),
            row.instrument.symbol,
            row.signal.strategy,
            _score_bar(row.signal.score),
            fmt_money(row.signal.price),
            "[green]✓[/green]" if row.signal.acted else "[dim]·[/dim]",
            row.signal.reason,
        )
    return table


def r_gauge(
    entry: Decimal,
    price: Decimal | None,
    stop: Decimal,
    target: Decimal,
    width: int = 11,
) -> str:
    """Where a trade sits between the stop it dies at and the target it takes.

    R is the engine's unit of account and is otherwise invisible as a shape:
    reading a position means holding four numbers in your head and doing the
    arithmetic. The gauge answers "where am I in this trade" at a glance.

    The scale is the trade's own risk, so the entry mark does not sit in the
    middle — on a 2R trade it sits a third of the way along, and seeing that is
    the point. Colour tracks distance above the stop rather than the sign of
    P&L: a trade barely above its stop needs attention whether or not it happens
    to be green.

    Box-drawing only. `trend_change` avoids block glyphs deliberately because
    they render inconsistently across fonts, and the Rich tables already depend
    on box-drawing, so this adds no new risk.
    """
    span = target - stop
    if price is None or span <= 0:
        return "[dim]" + "─" * width + "[/dim]"

    def slot(value: Decimal) -> int:
        fraction = float((value - stop) / span)
        return max(0, min(width - 1, round(fraction * (width - 1))))

    # One R above the stop is the entry, so this is "how many R of cushion".
    risk = entry - stop
    cushion = float((price - stop) / risk) if risk > 0 else 0.0
    if cushion <= 0.25:
        colour = "bright_red"
    elif cushion <= 0.5:
        colour = "red"
    elif cushion < 1.0:
        colour = "yellow"
    elif cushion < 2.0:
        colour = "green"
    else:
        colour = "bright_green"

    track = ["─"] * width
    track[slot(entry)] = "┼"
    cells = [f"[dim]{c}[/dim]" for c in track]

    # Outside the range is a real state — gapped through the stop, or past the
    # target between scans. Clamp the marker but say so, rather than drawing it
    # somewhere it is not.
    if price < stop:
        cells[0] = f"[{colour}]◀[/{colour}]"
    elif price > target:
        cells[width - 1] = f"[{colour}]▶[/{colour}]"
    else:
        cells[slot(price)] = f"[{colour}]●[/{colour}]"
    return "[dim]├[/dim]" + "".join(cells) + "[dim]┤[/dim]"


def _effective_stop(row: PositionRow) -> str:
    """The stop actually in force. An arrow marks the bars where the chandelier
    stop has overtaken the initial one — the trade can no longer give it all back."""
    initial = row.position.stop_price
    if row.stop_in_force > initial:
        return f"[green]↑{MONEY.format(row.stop_in_force)}[/green]"
    return fmt_money(initial)


def engine_positions_table(
    rows: list[PositionRow],
    title: str,
    show_exit: bool = False,
    max_positions: int | None = None,
    terminal_width: int | None = None,
) -> Table:
    """Open trades with live P&L and where the stop currently sits.

    `Opened` carries the time, not just the date: a day engine takes every entry
    intraday and must be flat by the bell, so the clock is the interesting part.
    The caption answers "is the book full?" — the fact that decides whether any
    new signal can be acted on at all, and previously only visible by counting
    rows by hand.
    """
    table = Table(title=title, title_justify="left")
    table.add_column("Symbol", style="bold")
    table.add_column("Strategy")
    table.add_column("Opened", style="dim")
    table.add_column("Qty", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Now", justify="right")
    table.add_column("Stop", justify="right")
    # Next to the stop it is derived from. Equal dollars committed do not mean
    # equal dollars risked, and this is the column where that becomes visible.
    table.add_column("Risk", justify="right")
    table.add_column("Target", justify="right")
    table.add_column("Bars", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("R", justify="right")
    # The gauge is the first thing to go when space runs short: it is a second
    # reading of numbers already in the row, and a truncated table loses data
    # silently, which is worse than losing decoration.
    show_gauge = terminal_width is None or terminal_width >= 160
    if show_gauge:
        table.add_column("stop → target", justify="center")
    if show_exit:
        table.add_column("Exit", style="dim")
    for row in rows:
        position, mark = row.position, row.mark
        r = position.r_multiple_at(mark)
        cells = [
            row.instrument.symbol,
            position.strategy,
            position.opened_at.strftime("%m-%d %H:%M"),
            fmt_qty(position.quantity),
            fmt_money(position.entry_price),
            fmt_money(mark),
            _effective_stop(row),
            fmt_money(row.risk_at_stop),
            fmt_money(position.target_price),
            str(position.bars_held),
            fmt_signed(position.pnl_at(mark)),
            f"{r:+.2f}R" if r is not None else "—",
        ]
        if show_gauge:
            cells.append(
                r_gauge(
                    position.entry_price,
                    mark,
                    # The stop actually in force, so the gauge shows the risk
                    # being run now rather than the one signed up for at entry.
                    max(position.stop_price, row.trail_stop),
                    position.target_price,
                )
            )
        if show_exit:
            cells.append(
                position.exit_reason or ""
                if position.status == PositionStatus.CLOSED
                else "[green]open[/green]"
            )
        table.add_row(*cells)

    open_rows = [r for r in rows if r.position.status != PositionStatus.CLOSED]
    if open_rows:
        committed = sum((r.position.cost for r in open_rows), Decimal(0))
        pnl = sum((r.position.pnl_at(r.mark) or Decimal(0) for r in open_rows), Decimal(0))
        held = (
            f"{len(open_rows)} of {max_positions} open"
            if max_positions
            else f"{len(open_rows)} open"
        )
        parts = [held]
        if max_positions is not None:
            room = max_positions - len(open_rows)
            parts.append(
                "no capacity — nothing new can be taken" if room <= 0 else f"room for {room}"
            )
        parts.append(f"{fmt_money(committed)} committed")
        risk = sum((r.risk_at_stop or Decimal(0) for r in open_rows), Decimal(0))
        parts.append(f"{fmt_money(risk)} at risk")
        parts.append(f"{fmt_signed(pnl)} unrealized")
        table.caption = "  ·  ".join(parts)
        table.caption_justify = "left"
    return table


def engine_report_table(stats: list[StrategyStat]) -> Table:
    """The scorecard the dry run exists to produce. Read expectancy first: a 40%
    win rate with +0.5R expectancy beats a 70% win rate that gives it all back."""
    table = Table(title="Strategy scorecard — closed trades", title_justify="left")
    table.add_column("Strategy", style="bold")
    table.add_column("Trades", justify="right")
    table.add_column("Open", justify="right")
    table.add_column("Win%", justify="right")
    table.add_column("W/L", justify="right")
    table.add_column("Avg win", justify="right")
    table.add_column("Avg loss", justify="right")
    table.add_column("Expectancy", justify="right")
    table.add_column("P&L", justify="right")
    for stat in stats:
        expectancy = (
            f"[{'green' if stat.expectancy_r >= 0 else 'red'}]{stat.expectancy_r:+.2f}R[/]"
            if stat.expectancy_r is not None
            else "—"
        )
        table.add_row(
            stat.strategy,
            str(stat.trades),
            str(stat.open_trades),
            f"{stat.win_rate:.0f}%" if stat.win_rate is not None else "—",
            f"{stat.wins}/{stat.losses}",
            fmt_signed_pct(stat.avg_win_pct),
            fmt_signed_pct(stat.avg_loss_pct),
            expectancy,
            fmt_signed(stat.total_pnl),
        )
    return table


def engine_strategies_table() -> Table:
    """What each rule looks for, in plain English."""
    table = Table(title="Entry strategies", title_justify="left")
    table.add_column("Key", style="bold")
    table.add_column("Name")
    table.add_column("Bars", justify="right")
    table.add_column("Looks for", style="dim")
    for key in sorted(STRATEGIES):
        strategy = STRATEGIES[key]
        table.add_row(key, strategy.name, str(strategy.min_bars), strategy.description)
    return table


def engine_exits_table(params: dict[str, float] | None = None) -> Table:
    """Exit rules in the order they are checked, with the live parameters."""
    table = Table(title="Exit rules (checked in this order)", title_justify="left")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Key", style="bold")
    table.add_column("Name")
    table.add_column("How it works", style="dim")
    for i, rule in enumerate(EXIT_RULES, start=1):
        table.add_row(str(i), rule.key, rule.name, rule.description)
    if params:
        table.caption = "  ".join(f"{k}={v:g}" for k, v in sorted(params.items()))
        table.caption_justify = "left"
    return table


def engine_backtest_renderables(
    result: EngineBacktestResult, width: int | None = None
) -> list[RenderableType]:
    """A backtest run: the headline, the same scorecard the live report uses
    (that sameness is the point — one scale for both), and the caveat, which is
    not decoration: the number is an upper bound."""
    out: list[RenderableType] = []
    ret = result.total_return_pct
    header = (
        f"[bold]engine backtest[/bold]  {result.start} → {result.end}  ·  "
        f"{len(result.symbols)} symbols  ·  fill: {result.fill}\n"
        f"{len(result.trades)} closed trades  ·  {result.open_at_end} still open  ·  "
        + (
            f"earnings blackout {result.earnings_blackout_days}d "
            f"({result.blackout_blocked} signals blocked)"
            if result.earnings_blackout_days
            else "earnings blackout off"
        )
        + f"\nequity {fmt_money(result.start_value)} → {fmt_money(result.end_value)}"
        + (f" ({ret:+.1f}%)" if ret is not None else "")
        + f"  ·  max drawdown {result.max_drawdown_pct:.1f}%"
    )
    out.append(Panel(header, expand=False, border_style="cyan"))
    if result.equity:
        # The shape matters as much as the endpoint: two runs can finish at the
        # same number, one climbing steadily and the other lurching through
        # drawdowns nobody would have held through.
        out.append(
            line_chart(
                [float(p.value) for p in result.equity],
                str(result.start),
                str(result.end),
                width=width or 64,
                baseline=float(result.start_value),
            )
        )
        out.append(Text(""))
    if result.stats:
        out.append(engine_report_table(result.stats))
    else:
        out.append(Text("No completed trades in this window.", style="dim"))
    if len(result.windows) > 1:
        out.append(engine_backtest_windows_table(result))
    for note in result.skipped:
        out.append(Text(f"  {note}", style="dim"))
    out.append(Panel(result.caveat, border_style="yellow", title="Caveat", title_align="left"))
    return out


def engine_backtest_windows_table(result: EngineBacktestResult) -> Table:
    """Expectancy by trailing window — the drift detector. Read across a row: a
    durable edge holds its grade in every column; a grade that fades toward the
    recent windows was earned in a market that may be gone. Cells under 30
    trades are dimmed: that is a vibe, not a measurement."""
    table = Table(title="Expectancy by window — does the edge still show up?", title_justify="left")
    table.add_column("Strategy", style="bold")
    for window in result.windows:
        table.add_column(window.label, justify="right")
    for key in sorted(result.strategies):
        cells = [key]
        for window in result.windows:
            stat = next((s for s in window.stats if s.strategy == key), None)
            if stat is None or stat.trades == 0 or stat.expectancy_r is None:
                cells.append("[dim]—[/dim]")
                continue
            grade = f"{stat.expectancy_r:+.2f}R ({stat.trades})"
            cells.append(f"[dim]{grade}[/dim]" if stat.trades < 30 else grade)
        table.add_row(*cells)
    table.caption = "trade counts in parentheses; dim = under 30 trades, read as noise"
    table.caption_justify = "left"
    return table


def engine_status_renderables(status: EngineStatus) -> list[RenderableType]:
    """What this engine is and whether it is healthy, in one screen.

    Ordered by the questions people actually ask, hardest first: which code and
    which database (a stale rollout once ran month-old rules for a full session),
    then the rule set, then whether it can trade, then whether the data supports
    the rules, then whether it is actually running.
    """
    mode = (
        f"day — flat at {status.flat_at_minute // 100:02d}:{status.flat_at_minute % 100:02d}"
        if status.day_mode
        else "swing — carries overnight"
    )
    mode += f"  ·  {status.timeframe} bars"
    header = f"[bold]{status.account}[/bold]  ·  {mode}\nbuild {status.build}\n{status.db_path}"
    out: list[RenderableType] = [Panel(header, expand=False, border_style="cyan")]

    rules = Table(title="Rule set", title_justify="left", show_header=False, box=None)
    rules.add_column(style="dim")
    rules.add_column()
    rules.add_row("strategies", ", ".join(status.strategies))
    rules.add_row("universe", f"{len(status.universe)} — {', '.join(status.universe)}")
    rules.add_row("sizing", f"{fmt_money(status.position_size)} per trade")
    rules.add_row(
        "earnings blackout",
        f"{status.earnings_blackout_days}d" if status.earnings_blackout_days else "off",
    )
    # Loud on purpose. An engine that quietly refuses every entry looks exactly
    # like a quiet market, and the only way to tell used to be reading the config.
    if status.regime_gated:
        gates = []
        if status.regime_sma:
            gates.append(f"SPY below its {status.regime_sma}-day")
        if status.regime_vix_max:
            gates.append(f"VIX above {status.regime_vix_max:g}")
        rules.add_row(
            "[yellow]regime gate[/yellow]",
            f"[yellow]on — no entries while {' or '.join(gates)}[/yellow]",
        )
    else:
        rules.add_row("regime gate", "off")
    out.append(rules)

    book = Table(title="Book", title_justify="left", show_header=False, box=None)
    book.add_column(style="dim")
    book.add_column()
    room = (
        "[yellow]full — no new entries until something exits[/yellow]"
        if status.capacity == 0
        else f"room for {status.capacity}"
    )
    book.add_row("positions", f"{status.open_positions} of {status.max_positions}  ·  {room}")
    book.add_row("committed", fmt_money(status.committed))
    # Committed capital reads as the exposure, and it is not: every position has
    # a stop under it. The share says how much of the committed money is actually
    # in play, which is the number that argues for one sizing mode over the other.
    share = (
        f" [dim]({status.risk_at_stop / status.committed * 100:.1f}% of committed, "
        "if every stop hits)[/dim]"
        if status.committed > 0
        else ""
    )
    book.add_row("at risk", fmt_money(status.risk_at_stop) + share)
    out.append(book)

    # Realized and unrealized separately, then the sum. An engine up only on open
    # positions while most of its completed trades lost money is a different
    # engine from one that is up on both, and a lone net number hides that.
    pnl = Table(title="P&L", title_justify="left", show_header=False, box=None)
    pnl.add_column(style="dim")
    pnl.add_column()
    closed = f" [dim]({status.closed_trades} closed)[/dim]" if status.closed_trades else ""
    pnl.add_row("realized", fmt_signed(status.realized) + closed)
    open_note = (
        f" [dim]({status.open_positions} open, marked at last close, not live)[/dim]"
        if status.open_positions
        else ""
    )
    pnl.add_row("unrealized", fmt_signed(status.unrealized) + open_note)
    pnl.add_row("[bold]net[/bold]", f"[bold]{fmt_signed(status.net_pnl)}[/bold]")
    out.append(pnl)

    data = Table(title="Data", title_justify="left", show_header=False, box=None)
    data.add_column(style="dim")
    data.add_column()
    span = (
        f"{status.bars_first} → {status.bars_last}"
        if status.bars_first and status.bars_last
        else "none"
    )
    data.add_row("bars", f"{status.bars_total:,}  ·  {span}")
    if status.short_history:
        listed = ", ".join(f"{s} ({n})" for s, n in status.short_history[:6])
        more = f" +{len(status.short_history) - 6} more" if len(status.short_history) > 6 else ""
        data.add_row(
            "[yellow]short[/yellow]",
            f"[yellow]{listed}{more}[/yellow] — rules need {status.warmup_bars} "
            + (
                f"(run 'trd sync --intraday {status.timeframe}')"
                if status.timeframe != "1d"
                else "(run 'trd sync --years 10')"
            ),
        )
    else:
        data.add_row("history", f"every symbol clears the {status.warmup_bars}-bar warmup")
    out.append(data)

    activity = Table(title="Activity", title_justify="left", show_header=False, box=None)
    activity.add_column(style="dim")
    activity.add_column()
    if status.last_scan is None:
        activity.add_row("last scan", "[yellow]never — run 'trd engine scan'[/yellow]")
    else:
        activity.add_row("last scan", status.last_scan.strftime("%Y-%m-%d %H:%M:%S"))
    activity.add_row("scans today", str(status.scans_today))
    out.append(activity)
    return out


def engine_runs_table(runs: list[EngineRun]) -> Table:
    """Scan history, newest first, with the interval between consecutive scans.

    The gaps are the point. A CronJob that stopped, a pod that failed, a scan
    that never fired — none of that shows up anywhere else, and "the engine did
    nothing" is indistinguishable from "the engine never ran" until you can see
    the cadence. Intervals more than double the usual one are called out.
    """
    table = Table(title="Engine scans — newest first", title_justify="left")
    table.add_column("Started", style="bold")
    table.add_column("Since prev", justify="right")
    table.add_column("Scanned", justify="right")
    table.add_column("Signals", justify="right")
    table.add_column("Opened", justify="right")
    table.add_column("Closed", justify="right")
    table.add_column("Mode", style="dim")

    gaps = [
        (runs[i].started_at - runs[i + 1].started_at).total_seconds() for i in range(len(runs) - 1)
    ]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else 0.0

    flagged = False
    for i, run in enumerate(runs):
        if i + 1 < len(runs):
            seconds = (run.started_at - runs[i + 1].started_at).total_seconds()
            text = f"{seconds / 60:.0f}m" if seconds >= 60 else f"{seconds:.0f}s"
            # A gap of more than twice the usual cadence means scans went missing.
            if typical and seconds > typical * 2:
                since, flagged = f"[yellow]{text} ⚠[/yellow]", True
            else:
                since = text
        else:
            since = "—"
        table.add_row(
            run.started_at.strftime("%m-%d %H:%M:%S"),
            since,
            str(run.scanned),
            str(run.signals),
            f"[green]{run.opened}[/green]" if run.opened else "0",
            f"[red]{run.closed}[/red]" if run.closed else "0",
            "paper" if run.paper else "signals only",
        )
    # Only explain the marker when one is actually on screen.
    if flagged:
        table.caption = f"usual cadence {typical / 60:.0f}m — ⚠ marks scans that went missing"
        table.caption_justify = "left"
    return table


def history_table(result: HistoryResult) -> Table:
    """What was bought and sold, newest first, and whether the period made money.

    Sells carry their realized result; buys do not, because a buy has not
    produced anything yet — its outcome lives in `trd portfolio` as unrealized
    P&L until the day it is sold.
    """
    window = f"since {result.since}" if result.since else "all time"
    table = Table(title=f"Transaction history — {window}", title_justify="left")
    table.add_column("Date", style="bold")
    table.add_column("Account", style="dim")
    table.add_column("Side")
    table.add_column("Symbol", style="bold")
    table.add_column("Qty", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Realized", justify="right")
    table.add_column("Note", style="dim")

    for row in result.rows:
        txn = row.txn
        buy = txn.side == Side.BUY
        realized = "—"
        if row.realized_pnl is not None:
            pct = f" ({row.realized_pct:+.1f}%)" if row.realized_pct is not None else ""
            realized = f"{fmt_signed(row.realized_pnl)}{pct}"
        table.add_row(
            txn.executed_at.strftime("%Y-%m-%d %H:%M"),
            row.account,
            "[green]buy[/green]" if buy else "[red]sell[/red]",
            row.instrument.symbol,
            fmt_qty(txn.quantity),
            fmt_money(txn.price),
            fmt_money(row.gross),
            realized,
            (txn.note or "")[:40],
        )

    parts = [f"{len(result.rows)} transactions"]
    parts.append(f"bought {fmt_money(result.bought)}")
    parts.append(f"sold {fmt_money(result.sold)}")
    if result.sells_with_result:
        pct = f" ({result.realized_pct:+.1f}%)" if result.realized_pct is not None else ""
        parts.append(f"realized {fmt_signed(result.realized_pnl)}{pct}")
    else:
        # Saying "realized 0.00" would read as breaking even rather than as
        # never having closed anything.
        parts.append("[dim]nothing sold — no realized result yet[/dim]")
    if result.fees:
        parts.append(f"fees {fmt_money(result.fees)}")
    table.caption = "  ·  ".join(parts)
    table.caption_justify = "left"
    return table


def engine_why_renderables(why: TradeExplanation) -> list[RenderableType]:
    """Why a trade was taken, what its vocabulary means, and where it gets out.

    Ordered for someone still learning the indicators: what happened, then the
    words needed to read what happened, then what would end it. The reason text
    is the one recorded at entry — it says what the rule saw that day, not what
    the same rule would say about today.
    """
    r = f"{why.r_multiple:+.2f}R" if why.r_multiple is not None else "—"
    header = (
        f"[bold]{why.symbol}[/bold] bought by [bold]{why.strategy_name}[/bold]  ·  "
        f"{why.opened_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"{fmt_qty(why.quantity)} @ {fmt_money(why.entry_price)}"
        + (f"  ·  now {fmt_money(why.price)}  ·  {r}" if why.price is not None else "")
        + f"  ·  1R = {fmt_money(why.risk_per_share)}/share"
        + f"  ·  held {why.bars_held} {'bar' if why.bars_held == 1 else 'bars'}"
    )
    out: list[RenderableType] = [Panel(header, expand=False, border_style="cyan")]

    out.append(
        Panel(
            f"[bold]What fired it[/bold]\n{why.reason}"
            + (
                f"\n\n[dim]score {why.score:.2f} — ranks same-bar candidates only[/dim]"
                if why.score is not None
                else ""
            ),
            border_style="green",
            title="Why this trade",
            title_align="left",
        )
    )
    if why.strategy_description:
        out.append(Text(f"  {why.strategy_description}", style="dim"))

    if why.glossary:
        terms = Table(
            title="What those words mean", title_justify="left", show_header=False, box=None
        )
        terms.add_column(style="bold cyan", no_wrap=True)
        terms.add_column()
        for key, term, definition in why.glossary:
            # Name the lookup key, not just the prose term: the next step is
            # `trd learn <key>`, and guessing the key from "R-multiple (R)" is
            # exactly the friction this view exists to remove.
            terms.add_row(term, definition.split("\n\n")[0] + f"\n[dim]trd learn {key}[/dim]")
        terms.caption = "each line ends with the command for the full formula and a worked example"
        terms.caption_justify = "left"
        out.append(terms)

    exits = Table(title="How it ends", title_justify="left")
    exits.add_column("Rule", style="bold")
    exits.add_column("Level", justify="right")
    exits.add_column("What it waits for", style="dim")
    for e in why.exits:
        name = f"[green]{e.name} ←[/green]" if e.in_force else e.name
        exits.add_row(name, fmt_money(e.level) if e.level is not None else "—", e.detail)
    exits.caption = "← marks the stop actually protecting the trade right now"
    exits.caption_justify = "left"
    out.append(exits)

    live = next((e.level for e in why.exits if e.in_force and e.level is not None), None)
    target = next((e.level for e in why.exits if e.rule == "target"), None)
    if live is not None and target is not None:
        gauge = r_gauge(why.entry_price, why.price, live, target, width=31)
        out.append(
            Text.from_markup(
                f"  {fmt_money(live)}  {gauge}  {fmt_money(target)}\n"
                "  [dim]│ the stop in force, where price sits now, and the target — "
                "┼ marks your entry[/dim]"
            )
        )
    return out


def engine_monitor_view(
    rows: list[PositionRow],
    max_positions: int,
    scans: int,
    last_scan: datetime,
    next_in: int,
    activity: list[str],
    build: str,
    label: str,
    day_mode: bool,
    terminal_width: int | None = None,
) -> RenderableType:
    """The screen you leave open while the market is on.

    A monitor that reprints a whole scan every pass makes you re-read
    everything to find the one thing that changed. This keeps the book still and
    lets the clock, the capacity and the activity feed move — so a fill is
    something you notice rather than something you scroll back to find.
    """
    open_rows = [r for r in rows if r.position.status != PositionStatus.CLOSED]
    capacity = max_positions - len(open_rows)
    room = "[yellow]full[/yellow]" if capacity <= 0 else f"[green]room for {capacity}[/green]"
    header = (
        f"[bold]{label}[/bold]  ·  {'day' if day_mode else 'swing'}  ·  [dim]{build}[/dim]\n"
        f"{last_scan.strftime('%H:%M:%S')}  ·  scan #{scans}  ·  next in {next_in}s  ·  "
        f"{len(open_rows)} of {max_positions} open  ·  {room}"
    )
    parts: list[RenderableType] = [Panel(header, expand=False, border_style="cyan")]

    if open_rows:
        parts.append(
            engine_positions_table(
                open_rows, "Open", max_positions=max_positions, terminal_width=terminal_width
            )
        )
    else:
        parts.append(Text("  flat — no open positions", style="dim"))

    feed = Table(title="Activity", title_justify="left", show_header=False, box=None)
    feed.add_column(style="dim", no_wrap=True)
    feed.add_column()
    if activity:
        for line in activity[:8]:
            stamp, _, rest = line.partition("|")
            feed.add_row(stamp, rest)
    else:
        # Quiet is the normal state — say so, rather than leaving a blank that
        # reads as something being broken.
        feed.add_row("", "[dim]nothing yet — most scans are quiet[/dim]")
    parts.append(feed)
    return Group(*parts)
