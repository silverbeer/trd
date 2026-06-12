from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from trd.cli.render import (
    board_table,
    dca_cadence_table,
    dca_history_table,
    dca_summary_table,
    dca_symbols_table,
    earnings_table,
    fmt_money,
    fmt_signed,
    fmt_signed_pct,
    indicator_panel,
    lots_table,
    positions_table,
)
from trd.config import DEFAULT_ACCOUNT, get_settings
from trd.db.connection import connect
from trd.errors import TrdError
from trd.models import AccountType, Side
from trd.providers import YFinanceProvider
from trd.repos import AccountRepo
from trd.services import (
    DcaDetailService,
    EarningsService,
    IndicatorService,
    PlanService,
    PortfolioService,
    SyncService,
    WatchlistService,
)
from trd.services.indicators import seed_defaults
from trd.services.plan import PlanStatus
from trd.services.watchlist import DEFAULT_WATCHLIST

app = typer.Typer(
    name="trd",
    help="Local-first investment tracker.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
watch_app = typer.Typer(help="Manage watchlists and the quote board.", no_args_is_help=True)
app.add_typer(watch_app, name="watch")
indicator_app = typer.Typer(help="Manage the followed-indicator list.", no_args_is_help=True)
app.add_typer(indicator_app, name="indicator")
sim_app = typer.Typer(
    help="Simulation account: paper-trade a monthly contribution.", no_args_is_help=True
)
app.add_typer(sim_app, name="sim")
plan_app = typer.Typer(
    help="DCA: recurring monthly investing on any account (real or paper).",
    no_args_is_help=True,
)
app.add_typer(plan_app, name="dca")
app.add_typer(plan_app, name="plan", hidden=True)  # back-compat alias
console = Console()
err_console = Console(stderr=True)


def _portfolio_service() -> PortfolioService:
    settings = get_settings()
    return PortfolioService(connect(settings.db_path), YFinanceProvider())


def _watchlist_service() -> WatchlistService:
    settings = get_settings()
    return WatchlistService(connect(settings.db_path), YFinanceProvider())


def _fail(exc: TrdError) -> None:
    err_console.print(f"[red]error:[/red] {exc}")
    raise typer.Exit(code=1)


def _parse_decimal(value: str, label: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation:
        err_console.print(f"[red]error:[/red] invalid {label}: {value!r}")
        raise typer.Exit(code=1) from None


def _parse_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        err_console.print(f"[red]error:[/red] invalid date: {value!r} (use YYYY-MM-DD)")
        raise typer.Exit(code=1) from None


AccountOpt = Annotated[str, typer.Option("--account", "-a", help="Account name.")]
PriceOpt = Annotated[
    str | None, typer.Option("--price", "-p", help="Execution price. Omit to use live quote.")
]
FeesOpt = Annotated[str, typer.Option("--fees", help="Commission/fees for the trade.")]
DateOpt = Annotated[
    str | None, typer.Option("--date", "-d", help="Execution date (YYYY-MM-DD). Default: now.")
]
NoteOpt = Annotated[str | None, typer.Option("--note", help="Free-form note.")]


@app.command()
def init() -> None:
    """Create the database, run migrations, and ensure the default account exists."""
    settings = get_settings()
    conn = connect(settings.db_path)
    service = PortfolioService(conn, YFinanceProvider())
    account = service.accounts.get_or_create(DEFAULT_ACCOUNT, AccountType.REAL)
    seeded = seed_defaults(conn)
    console.print(f"Database ready at [bold]{settings.db_path}[/bold]")
    console.print(f"Account [bold]{account.name}[/bold] ({account.type.value}) ready.")
    if seeded:
        console.print(f"Seeded [bold]{seeded}[/bold] default indicators (trd indicator ls).")


@app.command()
def sync(
    full: Annotated[
        bool, typer.Option("--full", help="Backfill 2 years of daily bars (default: last week).")
    ] = False,
    years: Annotated[
        int | None,
        typer.Option("--years", help="Backfill N years of daily bars (implies --full)."),
    ] = None,
) -> None:
    """Refresh quotes and daily price history for all tracked instruments."""
    settings = get_settings()
    conn = connect(settings.db_path)
    service = SyncService(conn, YFinanceProvider())
    with console.status("Syncing market data..."):
        result = service.sync(full=full, years=years)
    console.print(
        f"Synced [bold]{result.quotes}[/bold]/{result.instruments} quotes, "
        f"[bold]{result.bars}[/bold] daily bars, "
        f"[bold]{result.earnings}[/bold] earnings dates."
    )
    if result.failures:
        err_console.print(f"[yellow]warning:[/yellow] failed: {', '.join(result.failures)}")


@app.command()
def portfolio(
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Limit to one account.")
    ] = None,
    include_all: Annotated[
        bool, typer.Option("--all", help="Include simulation (paper) accounts.")
    ] = False,
) -> None:
    """Show holdings: quantity, cost basis, value, day change, P&L.

    Real money only by default — pass --all to include paper accounts.
    """
    service = _portfolio_service()
    try:
        with console.status("Fetching quotes..."):
            positions = service.positions(account, include_simulation=include_all)
    except TrdError as exc:
        _fail(exc)
        return
    if not positions:
        console.print("No open positions. Record one with [bold]trd buy[/bold].")
        return
    scope = account or ("all accounts" if include_all else "all real accounts")
    console.print(positions_table(positions, f"Portfolio — {scope}"))


@app.command()
def lots(
    symbol: Annotated[
        str | None, typer.Argument(help="Limit to one ticker. Omit for all positions.")
    ] = None,
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Limit to one account.")
    ] = None,
    include_all: Annotated[
        bool, typer.Option("--all", help="Include simulation (paper) accounts.")
    ] = False,
) -> None:
    """Per-purchase detail: buy date, price paid per share, total cost, gain since.

    Real money only by default — pass --all to include paper accounts.
    """
    service = _portfolio_service()
    try:
        with console.status("Fetching quotes..."):
            result = service.lots(account, symbol, include_simulation=include_all)
    except TrdError as exc:
        _fail(exc)
        return
    if not result:
        target = symbol.upper() if symbol else "anything"
        console.print(f"No open lots for {target}.")
        return
    parts = [p for p in (symbol.upper() if symbol else None, account) if p]
    title = (
        f"Lots — {', '.join(parts)}"
        if parts
        else ("Lots — all accounts" if include_all else "Lots — all real accounts")
    )
    console.print(lots_table(result, title))


@app.command()
def quote(symbol: Annotated[str, typer.Argument(help="Ticker, e.g. AAPL or BTC-USD.")]) -> None:
    """Live quote + instrument details for any symbol (tracked or not)."""
    provider = YFinanceProvider()
    try:
        with console.status(f"Fetching {symbol.upper()}..."):
            info = provider.get_info(symbol)
            q = provider.get_quote(symbol)
    except TrdError as exc:
        _fail(exc)
        return
    table = Table(title=f"{info.symbol} — {info.name or '?'}", title_justify="left")
    table.add_column("Field", style="dim")
    table.add_column("Value")
    table.add_row("Price", fmt_money(q.price))
    table.add_row("Prev close", fmt_money(q.prev_close))
    table.add_row("Day Δ%", fmt_signed_pct(q.day_change_pct))
    table.add_row("Type", info.type.value)
    table.add_row("Exchange", info.exchange or "—")
    table.add_row("Sector", info.sector or "—")
    table.add_row("Currency", info.currency)
    console.print(table)


def _trade(
    side: Side,
    symbol: str,
    quantity: str,
    price: str | None,
    account: str,
    fees: str,
    date_str: str | None,
    note: str | None,
) -> None:
    service = _portfolio_service()
    try:
        txn = service.record_trade(
            account_name=account,
            symbol=symbol,
            side=side,
            quantity=_parse_decimal(quantity, "quantity"),
            price=_parse_decimal(price, "price") if price is not None else None,
            fees=_parse_decimal(fees, "fees"),
            executed_at=_parse_date(date_str),
            note=note,
        )
    except TrdError as exc:
        _fail(exc)
        return
    verb = "Bought" if side == Side.BUY else "Sold"
    console.print(
        f"{verb} [bold]{txn.quantity.normalize():f} {symbol.upper()}[/bold] "
        f"@ {fmt_money(txn.price)} (fees {fmt_money(txn.fees)}) in {account}."
    )


@app.command()
def buy(
    symbol: Annotated[str, typer.Argument(help="Ticker, e.g. AAPL or BTC-USD.")],
    quantity: Annotated[str, typer.Argument(help="Shares/units. Fractions OK.")],
    price: PriceOpt = None,
    account: AccountOpt = DEFAULT_ACCOUNT,
    fees: FeesOpt = "0",
    date: DateOpt = None,
    note: NoteOpt = None,
) -> None:
    """Record a buy."""
    _trade(Side.BUY, symbol, quantity, price, account, fees, date, note)


@app.command()
def sell(
    symbol: Annotated[str, typer.Argument(help="Ticker, e.g. AAPL or BTC-USD.")],
    quantity: Annotated[str, typer.Argument(help="Shares/units. Fractions OK.")],
    price: PriceOpt = None,
    account: AccountOpt = DEFAULT_ACCOUNT,
    fees: FeesOpt = "0",
    date: DateOpt = None,
    note: NoteOpt = None,
) -> None:
    """Record a sell (validates you hold enough)."""
    _trade(Side.SELL, symbol, quantity, price, account, fees, date, note)


WatchListOpt = Annotated[
    str, typer.Option("--list", "-l", help=f"Watchlist name (default: {DEFAULT_WATCHLIST}).")
]


@watch_app.command("add")
def watch_add(
    symbol: Annotated[str, typer.Argument(help="Ticker to follow.")],
    list_name: WatchListOpt = DEFAULT_WATCHLIST,
) -> None:
    """Add a symbol to a watchlist (creates the list if needed)."""
    service = _watchlist_service()
    try:
        added = service.add(symbol, list_name)
    except TrdError as exc:
        _fail(exc)
        return
    if added:
        console.print(f"Watching [bold]{symbol.upper()}[/bold] on '{list_name}'.")
    else:
        console.print(f"[dim]{symbol.upper()} already on '{list_name}'.[/dim]")


@watch_app.command("rm")
def watch_rm(
    symbol: Annotated[str, typer.Argument(help="Ticker to stop following.")],
    list_name: WatchListOpt = DEFAULT_WATCHLIST,
) -> None:
    """Remove a symbol from a watchlist."""
    service = _watchlist_service()
    try:
        service.remove(symbol, list_name)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Removed [bold]{symbol.upper()}[/bold] from '{list_name}'.")


@watch_app.command("ls")
def watch_ls(
    list_name: Annotated[
        str | None, typer.Argument(help="Watchlist to show. Omit for all lists.")
    ] = None,
) -> None:
    """Quote board: price, day change, 52-week range position, volume vs average."""
    service = _watchlist_service()
    try:
        with console.status("Fetching quotes..."):
            rows = service.board(list_name)
    except TrdError as exc:
        _fail(exc)
        return
    if not rows:
        console.print("Nothing watched yet. Add with [bold]trd watch add SYMBOL[/bold].")
        return
    title = f"Watch — {list_name}" if list_name else "Watch — all lists"
    console.print(board_table(rows, title, show_list_column=list_name is None))


@app.command()
def earnings(
    days: Annotated[int, typer.Option("--days", "-d", help="Look-ahead window in days.")] = 14,
) -> None:
    """Upcoming earnings across portfolio and watchlists. Run 'trd sync' to refresh."""
    settings = get_settings()
    service = EarningsService(connect(settings.db_path))
    events = service.upcoming(days)
    if not events:
        console.print(f"No earnings in the next {days} days. Run [bold]trd sync[/bold] to refresh.")
        return
    console.print(earnings_table(events, days))


account_app = typer.Typer(
    help="Manage accounts (one per brokerage, plus simulation).", no_args_is_help=True
)
app.add_typer(account_app, name="account")


@account_app.command("add")
def account_add(
    name: Annotated[str, typer.Argument(help="Account name, e.g. fidelity, robinhood.")],
    type_: Annotated[
        str, typer.Option("--type", "-t", help="Account type: real or simulation.")
    ] = "real",
) -> None:
    """Create an account."""
    settings = get_settings()
    conn = connect(settings.db_path)
    repo = AccountRepo(conn)
    if type_ not in ("real", "simulation"):
        err_console.print(f"[red]error:[/red] type must be 'real' or 'simulation', got {type_!r}")
        raise typer.Exit(code=1)
    if repo.get_by_name(name) is not None:
        console.print(f"[dim]Account '{name}' already exists.[/dim]")
        return
    account = repo.create(name, AccountType(type_))
    console.print(f"Account [bold]{account.name}[/bold] ({account.type.value}) created.")


@account_app.command("ls")
def account_ls() -> None:
    """List accounts."""
    settings = get_settings()
    conn = connect(settings.db_path)
    accounts = AccountRepo(conn).list_all()
    if not accounts:
        console.print("No accounts. Run [bold]trd init[/bold].")
        return
    table = Table(title="Accounts", title_justify="left")
    table.add_column("Name", style="bold")
    table.add_column("Type")
    table.add_column("Currency")
    for account in accounts:
        table.add_row(account.name, account.type.value, account.currency)
    console.print(table)


def _indicator_service() -> IndicatorService:
    settings = get_settings()
    return IndicatorService(connect(settings.db_path))


def _parse_params(params: list[str]) -> dict:
    out: dict = {}
    for raw in params:
        if "=" not in raw:
            err_console.print(f"[red]error:[/red] params look like name=value, got {raw!r}")
            raise typer.Exit(code=1)
        name, value = raw.split("=", 1)
        try:
            out[name.strip()] = int(value)
        except ValueError:
            try:
                out[name.strip()] = float(value)
            except ValueError:
                err_console.print(f"[red]error:[/red] param {name} needs a number, got {value!r}")
                raise typer.Exit(code=1) from None
    return out


ParamOpt = Annotated[
    list[str] | None, typer.Option("--param", "-p", help="Override, e.g. -p period=21. Repeatable.")
]


@app.command()
def indicators(symbol: Annotated[str, typer.Argument(help="Tracked ticker.")]) -> None:
    """Indicator panel with plain-English readings (learning mode)."""
    service = _indicator_service()
    try:
        rows = service.panel(symbol)
    except TrdError as exc:
        _fail(exc)
        return
    if not rows:
        console.print("No indicators followed. Run [bold]trd init[/bold] to seed defaults.")
        return
    console.print(indicator_panel(rows, symbol.upper()))
    console.print("[dim]every reading explained: trd indicator info <key> · trd learn[/dim]")


@indicator_app.command("ls")
def indicator_ls() -> None:
    """The followed-indicator list (your evolving set)."""
    service = _indicator_service()
    for warning in service.validate_configs():
        err_console.print(f"[yellow]warning:[/yellow] {warning}")
    configs = service.configs.list_all()
    if not configs:
        console.print(
            "Nothing followed. Run [bold]trd init[/bold] or [bold]trd indicator add[/bold]."
        )
        return
    table = Table(title="Followed indicators", title_justify="left")
    table.add_column("Key", style="bold")
    table.add_column("Params")
    table.add_column("Enabled")
    table.add_column("Note")
    for config in configs:
        params = ", ".join(f"{k}={v}" for k, v in config.params.items()) or "—"
        table.add_row(
            config.key,
            params,
            "[green]yes[/green]" if config.enabled else "[dim]no[/dim]",
            config.note or "",
        )
    console.print(table)


@indicator_app.command("catalog")
def indicator_catalog() -> None:
    """Everything available in the code registry."""
    service = _indicator_service()
    table = Table(title="Indicator catalog", title_justify="left")
    table.add_column("Key", style="bold")
    table.add_column("Name")
    table.add_column("Category")
    table.add_column("Default params")
    for indicator in service.catalog():
        params = ", ".join(f"{k}={v}" for k, v in indicator.default_params.items()) or "—"
        table.add_row(indicator.key, indicator.name, indicator.category.value, params)
    console.print(table)


@indicator_app.command("add")
def indicator_add(
    key: Annotated[str, typer.Argument(help="Registry key, e.g. rsi. See 'indicator catalog'.")],
    param: ParamOpt = None,
    note: NoteOpt = None,
) -> None:
    """Follow an indicator (same key twice with different params is fine)."""
    service = _indicator_service()
    try:
        config = service.add(key, _parse_params(param or []), note=note)
    except TrdError as exc:
        _fail(exc)
        return
    params = ", ".join(f"{k}={v}" for k, v in config.params.items()) or "defaults"
    console.print(f"Following [bold]{config.key}[/bold] ({params}).")


@indicator_app.command("rm")
def indicator_rm(
    key: Annotated[str, typer.Argument(help="Registry key to stop following.")],
    param: ParamOpt = None,
) -> None:
    """Stop following (soft-disable — note and history kept)."""
    service = _indicator_service()
    try:
        count = service.remove(key, _parse_params(param) if param else None)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Disabled [bold]{count}[/bold] config(s) for '{key}'. History kept.")


@indicator_app.command("info")
def indicator_info(
    key: Annotated[str, typer.Argument(help="Registry key, e.g. macd.")],
) -> None:
    """Full description + interpretation guide for one indicator."""
    from trd.indicators import REGISTRY

    indicator = REGISTRY.get(key)
    if indicator is None:
        err_console.print(f"[red]error:[/red] no indicator '{key}'. See 'trd indicator catalog'.")
        raise typer.Exit(code=1)
    console.print(f"[bold]{indicator.name}[/bold] ({indicator.key}) — {indicator.category.value}")
    params = ", ".join(f"{k}={v}" for k, v in indicator.default_params.items()) or "none"
    console.print(f"[dim]Params:[/dim] {params}")
    console.print(f"[dim]Components:[/dim] {', '.join(indicator.components)}")
    console.print(indicator.description)


def _plan_service() -> PlanService:
    settings = get_settings()
    return PlanService(connect(settings.db_path), YFinanceProvider())


def _parse_allocs(alloc: list[str] | None) -> dict[str, Decimal] | None:
    if not alloc:
        return None
    allocations: dict[str, Decimal] = {}
    for raw in alloc:
        if "=" not in raw:
            err_console.print(f"[red]error:[/red] --alloc looks like SYMBOL=WEIGHT, got {raw!r}")
            raise typer.Exit(code=1)
        symbol, weight = raw.split("=", 1)
        allocations[symbol.strip().upper()] = _parse_decimal(weight, "allocation weight")
    return allocations


MonthlyOpt = Annotated[str, typer.Option("--monthly", "-m", help="Contribution per month.")]
StrategyOpt = Annotated[
    str,
    typer.Option(
        "--strategy",
        "-s",
        help="'ticker' (fixed buy), 'momentum' (best 3-month watchlist performer), "
        "or 'allocation' (implied by --alloc).",
    ),
]
TickerOpt = Annotated[str, typer.Option("--ticker", "-t", help="Symbol for the 'ticker' strategy.")]
AllocOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--alloc",
        help="Split the monthly amount: --alloc SPY=30 --alloc QQQ=70 (weights sum to 100).",
    ),
]
PlanAccountOpt = Annotated[
    str | None,
    typer.Option(
        "--account", "-a", help="Account with the plan. Optional if only one plan exists."
    ),
]
PlanDateOpt = Annotated[
    str | None,
    typer.Option(
        "--date", "-d", help="Backdate (YYYY-MM-DD) using historical close — builds past months."
    ),
]


def _print_invest(service: PlanService, txns: list) -> None:
    for txn in txns:
        instrument = service.instruments.get(txn.instrument_id)
        symbol = instrument.symbol if instrument else "?"
        console.print(
            f"Recorded [bold]{txn.quantity.normalize():f} {symbol}[/bold] "
            f"@ {fmt_money(txn.price)} ({txn.executed_at.date()})."
        )


def _print_status(status: PlanStatus, title: str) -> None:
    table = Table(title=title, title_justify="left")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    table.add_row("Strategy", status.plan.strategy_label)
    if status.plan.note:
        table.add_row("Goal", status.plan.note)
    table.add_row("Monthly", fmt_money(status.plan.monthly_amount))
    table.add_row("Months invested", str(status.months_invested))
    table.add_row("Total invested", fmt_money(status.invested))
    table.add_row("Current value", fmt_money(status.value))
    table.add_row("P&L", fmt_signed(status.pl))
    table.add_row("P&L %", fmt_signed_pct(status.pl_pct))
    if status.benchmark_value is not None:
        table.add_row("SPY same dates", fmt_money(status.benchmark_value))
        table.add_row("vs SPY", fmt_signed(status.vs_benchmark))
    else:
        table.add_row("SPY same dates", "[dim]needs SPY history — trd sync --full[/dim]")
    console.print(table)


@plan_app.command("set")
def plan_set(
    account: Annotated[
        str, typer.Option("--account", "-a", help="Existing account (real or sim).")
    ],
    monthly: MonthlyOpt = "100",
    strategy: StrategyOpt = "ticker",
    ticker: TickerOpt = "SPY",
    alloc: AllocOpt = None,
    note: NoteOpt = None,
    day: Annotated[
        int | None, typer.Option("--day", help="Scheduled buy day of month (1-31), e.g. 15.")
    ] = None,
) -> None:
    """Attach a monthly DCA plan to an account.

    For real accounts: you execute the buys at your broker; trd records and scores them.
    """
    service = _plan_service()
    try:
        plan = service.set_plan(
            account,
            _parse_decimal(monthly, "monthly amount"),
            strategy,
            ticker,
            _parse_allocs(alloc),
            note=note,
            day_of_month=day,
        )
    except TrdError as exc:
        _fail(exc)
        return
    kind = "paper" if plan.is_paper else "real money — execute at your broker, trd records"
    console.print(
        f"Plan on [bold]{account}[/bold]: {fmt_money(plan.monthly_amount)}/month "
        f"into {plan.strategy_label} ({kind})."
    )


@plan_app.command("invest")
def plan_invest(
    account: PlanAccountOpt = None,
    date_str: PlanDateOpt = None,
) -> None:
    """Record this month's contribution (once per month per plan)."""
    service = _plan_service()
    when = _parse_date(date_str)
    try:
        name = account or service.resolve_default_account()
        txns = service.invest(name, when.date() if when else None)
    except TrdError as exc:
        _fail(exc)
        return
    _print_invest(service, txns)
    plan = service.get_plan(name)
    if not plan.is_paper:
        console.print(
            "[dim]Reminder: trd records only — make the matching buy at your broker.[/dim]"
        )


@plan_app.command("status")
def plan_status(account: PlanAccountOpt = None) -> None:
    """Plan performance: invested vs value vs what SPY would have done."""
    service = _plan_service()
    try:
        name = account or service.resolve_default_account()
        with console.status("Fetching quotes..."):
            status = service.status(name)
    except TrdError as exc:
        _fail(exc)
        return
    _print_status(status, f"Plan — {name}")
    console.print("[dim]terms: trd learn pl · benchmark · dca[/dim]")


@plan_app.command("ls")
def plan_ls() -> None:
    """All contribution plans."""
    service = _plan_service()
    plans = service.list_plans()
    if not plans:
        console.print("No plans. Run [bold]trd plan set[/bold] or [bold]trd sim init[/bold].")
        return
    table = Table(title="DCA plans", title_justify="left")
    table.add_column("Account", style="bold")
    table.add_column("Type")
    table.add_column("Monthly", justify="right")
    table.add_column("Day", justify="right")
    table.add_column("Active")
    table.add_column("Strategy")
    table.add_column("Goal")
    for plan in plans:
        table.add_row(
            plan.account.name,
            "paper" if plan.is_paper else "real",
            fmt_money(plan.monthly_amount),
            str(plan.day_of_month) if plan.day_of_month else "—",
            "[green]yes[/green]" if plan.active else "[yellow]paused[/yellow]",
            plan.strategy_label,
            plan.note or "—",
        )
    console.print(table)


def _dca_detail_service() -> DcaDetailService:
    settings = get_settings()
    return DcaDetailService(connect(settings.db_path), YFinanceProvider())


@plan_app.command("show")
def plan_show(account: PlanAccountOpt = None) -> None:
    """The full picture: summary + XIRR, per-symbol stats with drift, cadence."""
    service = _dca_detail_service()
    try:
        name = account or service.plans.resolve_default_account()
        with console.status("Fetching quotes..."):
            detail = service.detail(name)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(dca_summary_table(detail))
    if detail.symbol_stats:
        console.print(dca_symbols_table(detail))
        console.print(dca_cadence_table(detail))
    console.print("[dim]terms: trd learn xirr · drift · cost-basis · dca[/dim]")


@plan_app.command("history")
def plan_history(
    account: PlanAccountOpt = None,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Show only the last N months.")
    ] = None,
) -> None:
    """Every contribution event: date, legs, prices paid."""
    service = _dca_detail_service()
    try:
        name = account or service.plans.resolve_default_account()
        with console.status("Loading..."):
            detail = service.detail(name)
    except TrdError as exc:
        _fail(exc)
        return
    if not detail.events:
        console.print("No contributions yet. Run [bold]trd dca invest[/bold].")
        return
    console.print(dca_history_table(detail, limit))


@plan_app.command("edit")
def plan_edit(
    account: PlanAccountOpt = None,
    monthly: Annotated[
        str | None, typer.Option("--monthly", "-m", help="New contribution per month.")
    ] = None,
    day: Annotated[
        int | None, typer.Option("--day", help="Scheduled buy day of month (1-31).")
    ] = None,
    note: NoteOpt = None,
) -> None:
    """Update an existing plan's amount, scheduled day, or goal note."""
    service = _plan_service()
    try:
        name = account or service.resolve_default_account()
        plan = service.update_plan(
            name,
            monthly=_parse_decimal(monthly, "monthly amount") if monthly else None,
            day_of_month=day,
            note=note,
        )
    except TrdError as exc:
        _fail(exc)
        return
    day_text = f", day {plan.day_of_month}" if plan.day_of_month else ""
    console.print(f"Plan on [bold]{name}[/bold]: {fmt_money(plan.monthly_amount)}/month{day_text}.")


@plan_app.command("pause")
def plan_pause(account: PlanAccountOpt = None) -> None:
    """Pause a plan — invest is blocked until resumed. History kept."""
    service = _plan_service()
    try:
        name = account or service.resolve_default_account()
        service.pause(name)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Plan on [bold]{name}[/bold] paused.")


@plan_app.command("resume")
def plan_resume(account: PlanAccountOpt = None) -> None:
    """Resume a paused plan."""
    service = _plan_service()
    try:
        name = account or service.resolve_default_account()
        service.resume(name)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Plan on [bold]{name}[/bold] active again.")


SimNameOpt = Annotated[str, typer.Option("--name", help="Simulation account name.")]


@sim_app.command("init")
def sim_init(
    monthly: MonthlyOpt = "100",
    strategy: StrategyOpt = "ticker",
    ticker: TickerOpt = "SPY",
    alloc: AllocOpt = None,
    note: NoteOpt = None,
    name: SimNameOpt = "sim",
) -> None:
    """Create a paper (simulation) account with a monthly plan."""
    service = _plan_service()
    try:
        plan = service.set_plan(
            name,
            _parse_decimal(monthly, "monthly amount"),
            strategy,
            ticker,
            _parse_allocs(alloc),
            create_simulation=True,
            note=note,
        )
    except TrdError as exc:
        _fail(exc)
        return
    console.print(
        f"Simulation account [bold]{name}[/bold]: {fmt_money(plan.monthly_amount)}/month "
        f"into {plan.strategy_label}. Run [bold]trd sim invest[/bold] monthly."
    )


@sim_app.command("invest")
def sim_invest(
    date_str: PlanDateOpt = None,
    name: SimNameOpt = "sim",
) -> None:
    """Execute this month's paper contribution (once per month)."""
    service = _plan_service()
    when = _parse_date(date_str)
    try:
        txns = service.invest(name, when.date() if when else None)
    except TrdError as exc:
        _fail(exc)
        return
    _print_invest(service, txns)


@sim_app.command("status")
def sim_status(name: SimNameOpt = "sim") -> None:
    """Performance: invested vs value vs what SPY would have done."""
    service = _plan_service()
    try:
        with console.status("Fetching quotes..."):
            status = service.status(name)
    except TrdError as exc:
        _fail(exc)
        return
    _print_status(status, f"Simulation — {name}")


@app.command()
def learn(
    term: Annotated[
        str | None,
        typer.Argument(help="Term to explain, e.g. xirr, drift, fifo. Omit to list all."),
    ] = None,
) -> None:
    """The investing dictionary: every term trd shows, every formula trd computes."""
    from trd.learn import all_entries, lookup

    if term is None:
        table = Table(title="trd learn — investing dictionary", title_justify="left")
        table.add_column("Term", style="bold")
        table.add_column("Category", style="dim")
        table.add_column("What it is")
        last_category = None
        for entry in all_entries():
            if entry.category != last_category:
                table.add_section()
                last_category = entry.category
            table.add_row(entry.key, entry.category.value, entry.term)
        console.print(table)
        console.print(
            "[dim]trd learn <term> for the definition, formula, and a worked example.[/dim]"
        )
        return

    result = lookup(term)
    if isinstance(result, list):
        if not result:
            err_console.print(f"[red]error:[/red] no term '{term}'. See 'trd learn'.")
            raise typer.Exit(code=1)
        if len(result) > 1:
            console.print(f"Did you mean: {', '.join(e.key for e in result)}")
            return
        result = result[0]
    console.print(f"[bold]{result.term}[/bold] [dim]({result.category.value})[/dim]\n")
    console.print(result.definition)
    if result.formula:
        console.print(
            f"\n[bold]Formula (exactly what trd computes):[/bold]\n[cyan]{result.formula}[/cyan]"
        )
    if result.example:
        console.print(f"\n[bold]Example:[/bold] {result.example}")
    if result.used_in:
        console.print(f"\n[dim]Appears in: {', '.join(result.used_in)}[/dim]")
    if result.related:
        console.print(f"[dim]Related: {', '.join(result.related)}[/dim]")


@app.command(name="import")
def import_csv(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, help="CSV of transactions.")],
) -> None:
    """Bulk-load transactions. Columns: date,account,symbol,side,quantity,price[,fees,note]."""
    service = _portfolio_service()
    try:
        count = service.import_csv(path)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Imported [bold]{count}[/bold] transactions from {path}.")


if __name__ == "__main__":
    app()
