from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from trd.cli.render import (
    board_table,
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
    EarningsService,
    IndicatorService,
    PortfolioService,
    SimService,
    SyncService,
    WatchlistService,
)
from trd.services.indicators import seed_defaults
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
) -> None:
    """Refresh quotes and daily price history for all tracked instruments."""
    settings = get_settings()
    conn = connect(settings.db_path)
    service = SyncService(conn, YFinanceProvider())
    with console.status("Syncing market data..."):
        result = service.sync(full=full)
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
) -> None:
    """Show holdings: quantity, cost basis, value, day change, P&L."""
    service = _portfolio_service()
    try:
        with console.status("Fetching quotes..."):
            positions = service.positions(account)
    except TrdError as exc:
        _fail(exc)
        return
    if not positions:
        console.print("No open positions. Record one with [bold]trd buy[/bold].")
        return
    title = f"Portfolio — {account}" if account else "Portfolio — all accounts"
    console.print(positions_table(positions, title))


@app.command()
def lots(
    symbol: Annotated[
        str | None, typer.Argument(help="Limit to one ticker. Omit for all positions.")
    ] = None,
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Limit to one account.")
    ] = None,
) -> None:
    """Per-purchase detail: buy date, price paid per share, total cost, gain since."""
    service = _portfolio_service()
    try:
        with console.status("Fetching quotes..."):
            result = service.lots(account, symbol)
    except TrdError as exc:
        _fail(exc)
        return
    if not result:
        target = symbol.upper() if symbol else "anything"
        console.print(f"No open lots for {target}.")
        return
    parts = [p for p in (symbol.upper() if symbol else None, account) if p]
    title = f"Lots — {', '.join(parts)}" if parts else "Lots — all accounts"
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


def _sim_service() -> SimService:
    settings = get_settings()
    return SimService(connect(settings.db_path), YFinanceProvider())


SimNameOpt = Annotated[str, typer.Option("--name", help="Simulation account name.")]


@sim_app.command("init")
def sim_init(
    monthly: Annotated[
        str, typer.Option("--monthly", "-m", help="Contribution per month.")
    ] = "100",
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            "-s",
            help="'ticker' (fixed buy) or 'momentum' (best 3-month watchlist performer).",
        ),
    ] = "ticker",
    ticker: Annotated[
        str, typer.Option("--ticker", "-t", help="Symbol for the 'ticker' strategy.")
    ] = "SPY",
    name: SimNameOpt = "sim",
) -> None:
    """Create the simulation account."""
    service = _sim_service()
    try:
        config = service.init(_parse_decimal(monthly, "monthly amount"), strategy, ticker, name)
    except TrdError as exc:
        _fail(exc)
        return
    detail = config.strategy_ticker if config.strategy == "ticker" else "momentum pick"
    console.print(
        f"Simulation account [bold]{name}[/bold]: {fmt_money(config.monthly_amount)}/month "
        f"into {detail}. Run [bold]trd sim invest[/bold] monthly."
    )


@sim_app.command("invest")
def sim_invest(
    date_str: Annotated[
        str | None,
        typer.Option(
            "--date",
            "-d",
            help="Backdate (YYYY-MM-DD) using historical close — builds past months.",
        ),
    ] = None,
    name: SimNameOpt = "sim",
) -> None:
    """Execute this month's contribution (strategy-driven, once per month)."""
    service = _sim_service()
    when = _parse_date(date_str)
    try:
        txn, symbol = service.invest(name, when.date() if when else None)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(
        f"Sim bought [bold]{txn.quantity.normalize():f} {symbol}[/bold] @ {fmt_money(txn.price)} "
        f"({txn.executed_at.date()})."
    )


@sim_app.command("status")
def sim_status(name: SimNameOpt = "sim") -> None:
    """Performance: invested vs value vs what SPY would have done."""
    service = _sim_service()
    try:
        with console.status("Fetching quotes..."):
            status = service.status(name)
    except TrdError as exc:
        _fail(exc)
        return
    table = Table(title=f"Simulation — {name}", title_justify="left")
    table.add_column("Metric", style="dim")
    table.add_column("Value", justify="right")
    strategy = (
        status.config.strategy_ticker
        if status.config.strategy == "ticker"
        else "momentum (watchlist)"
    )
    table.add_row("Strategy", strategy or "—")
    table.add_row("Monthly", fmt_money(status.config.monthly_amount))
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
