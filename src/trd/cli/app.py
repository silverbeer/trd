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
    fmt_signed_pct,
    lots_table,
    positions_table,
)
from trd.config import DEFAULT_ACCOUNT, get_settings
from trd.db.connection import connect
from trd.errors import TrdError
from trd.models import AccountType, Side
from trd.providers import YFinanceProvider
from trd.repos import AccountRepo
from trd.services import EarningsService, PortfolioService, SyncService, WatchlistService
from trd.services.watchlist import DEFAULT_WATCHLIST

app = typer.Typer(
    name="trd",
    help="Local-first investment tracker.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
watch_app = typer.Typer(help="Manage watchlists and the quote board.", no_args_is_help=True)
app.add_typer(watch_app, name="watch")
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
    console.print(f"Database ready at [bold]{settings.db_path}[/bold]")
    console.print(f"Account [bold]{account.name}[/bold] ({account.type.value}) ready.")


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
