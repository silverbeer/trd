import json as _json
import sys
from contextlib import nullcontext, suppress
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from trd.build import build_version
from trd.cli.render import (
    backtest_table,
    board_table,
    dashboard_allocation_table,
    dashboard_card,
    dashboard_movers,
    dca_cadence_table,
    dca_history_table,
    dca_summary_table,
    dca_symbols_table,
    earnings_table,
    engine_backtest_renderables,
    engine_exits_table,
    engine_positions_table,
    engine_report_table,
    engine_runs_table,
    engine_scan_renderables,
    engine_signals_table,
    engine_status_renderables,
    engine_strategies_table,
    engine_why_renderables,
    equity_curve_renderables,
    equity_daily_table,
    exit_table,
    fmt_money,
    fmt_signed,
    fmt_signed_pct,
    forecast_table,
    history_table,
    indicator_panel,
    lots_table,
    movers_table,
    plans_pnl_table,
    positions_table,
    prep_history_table,
    sunday_prep_markdown,
    sunday_prep_renderables,
)
from trd.config import DEFAULT_ACCOUNT, get_settings
from trd.db.connection import connect
from trd.errors import TrdError
from trd.models import AccountType, Side, SizingMode
from trd.notify import label_from_env, scan_messages
from trd.notify.telegram import from_env as notify_from_env
from trd.providers import YFinanceProvider
from trd.repos import AccountRepo
from trd.services import (
    DashboardService,
    DcaDetailService,
    DcaProjectionService,
    EarningsService,
    EngineService,
    EquityCurveService,
    ExitTriggerService,
    HistoryService,
    IndicatorService,
    MoversService,
    PlanService,
    PortfolioService,
    PrepHistoryService,
    SundayPrepBriefing,
    SundayPrepService,
    SyncService,
    WatchlistService,
)
from trd.services.backtest import BacktestService, FillMode
from trd.services.engine import (
    DEFAULT_EARNINGS_BLACKOUT_DAYS,
    DEFAULT_ENGINE_ACCOUNT,
    ScanResult,
    scan_events,
)
from trd.services.indicators import seed_defaults
from trd.services.plan import PlanStatus
from trd.services.watchlist import DEFAULT_WATCHLIST

app = typer.Typer(
    name="trd",
    help="Local-first investment tracker.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,  # main() renders TrdError cleanly instead
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
exit_app = typer.Typer(help="Stop/target exit triggers on holdings.", no_args_is_help=True)
app.add_typer(exit_app, name="exit")
engine_app = typer.Typer(
    help="Monitor-mode trading engine: scan a universe, paper-trade the signals.",
    no_args_is_help=True,
)
app.add_typer(engine_app, name="engine")
console = Console()
err_console = Console(stderr=True)


def _portfolio_service() -> PortfolioService:
    settings = get_settings()
    return PortfolioService(connect(settings.db_path), YFinanceProvider())


def _watchlist_service() -> WatchlistService:
    settings = get_settings()
    return WatchlistService(connect(settings.db_path), YFinanceProvider())


def _exit_service() -> ExitTriggerService:
    settings = get_settings()
    return ExitTriggerService(connect(settings.db_path), YFinanceProvider())


def _engine_service() -> EngineService:
    settings = get_settings()
    return EngineService(connect(settings.db_path), YFinanceProvider())


def _prefetched_quotes(symbols_of) -> dict:
    """Fetch quotes with the database closed, then hand them to the real call.

    DuckDB allows one writer, so any connection this process holds blocks every
    other reader. The provider call takes seconds and touches no database, so
    doing it on an open connection is several seconds of lock held for nothing —
    which is what made the CLI unusable against a live engine during market hours.

    Two short connections either side of the network call cost about 20ms each.
    """
    service = _engine_service()
    try:
        symbols = symbols_of(service)
    finally:
        service.conn.close()
    if not symbols:
        return {}
    return service._quotes(symbols)


# Whether this invocation speaks JSON. Set by the command, read by the error
# paths — an agent that asked for JSON must get JSON when things go wrong too,
# or every failure looks like a corrupt response instead of a handled state.
_json_mode = False

JsonOpt = Annotated[
    bool,
    typer.Option("--json", help="Emit as JSON: full precision, stable keys, no truncation."),
]


@app.callback()
def _reset_output_mode() -> None:
    """Typer runs this before every command. Resets the JSON flag so one
    invocation's mode cannot leak into the next — which happens whenever the app
    is driven in-process rather than as a fresh subprocess, as the tests do."""
    global _json_mode
    _json_mode = False


def _json_requested() -> bool:
    """Whether this invocation asked for JSON, read from argv rather than from the
    flag a command sets.

    An error can escape before any command body runs — a database lock taken
    during connect, for instance — and that failure still has to answer in the
    format the caller asked for. argv is the only source that is already true at
    that point, and it cannot leak between in-process invocations.
    """
    return "--json" in sys.argv[1:]


def _use_json(enabled: bool) -> bool:
    """Record that this invocation speaks JSON, and hand the flag back."""
    global _json_mode
    _json_mode = enabled
    return enabled


def _spinner(message: str):
    """A Rich status spinner writes to stdout, which would corrupt a JSON
    document mid-stream. Silence in JSON mode; a spinner for humans."""
    return nullcontext() if _json_mode else console.status(message)


def _emit_json(payload: Any) -> None:
    """Machine output: stdout, one document, no colour and no highlighting.

    Deliberately not `console.print_json`, which pretty-prints with ANSI colour
    and re-wraps to the terminal width — both of which corrupt a pipe.
    """
    if hasattr(payload, "model_dump_json"):
        print(payload.model_dump_json())  # type: ignore[attr-defined]
        return
    if isinstance(payload, list) and payload and hasattr(payload[0], "model_dump_json"):
        print("[" + ",".join(item.model_dump_json() for item in payload) + "]")
        return
    print(_json.dumps(payload, default=str, separators=(",", ":")))


def _fail(exc: TrdError) -> None:
    if _json_mode:
        # On stdout on purpose, so one `| jq` handles success and failure alike.
        # The non-zero exit is what distinguishes them.
        print(
            _json.dumps({"error": type(exc).__name__, "message": str(exc)}, separators=(",", ":"))
        )
        raise typer.Exit(code=1)
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
    with _spinner("Syncing market data..."):
        result = service.sync(full=full, years=years)
    console.print(
        f"Synced [bold]{result.quotes}[/bold]/{result.instruments} quotes, "
        f"[bold]{result.bars}[/bold] daily bars, "
        f"[bold]{result.earnings}[/bold] earnings dates."
    )
    if result.failures:
        err_console.print(f"[yellow]warning:[/yellow] failed: {', '.join(result.failures)}")


@app.command()
def dashboard(
    full: Annotated[
        bool, typer.Option("--full", help="Show allocation, concentration, and full movers.")
    ] = False,
    include_all: Annotated[
        bool, typer.Option("--all", help="Include simulation (paper) accounts.")
    ] = False,
) -> None:
    """Home view: value, return, XIRR, vs S&P 500, today, top holding, movers."""
    settings = get_settings()
    service = DashboardService(connect(settings.db_path), YFinanceProvider())
    try:
        with _spinner("Building dashboard..."):
            dash = service.summary(include_simulation=include_all)
    except TrdError as exc:
        _fail(exc)
        return
    if dash.value == 0 and not dash.holdings:
        console.print("No holdings yet. Record a buy with [bold]trd buy[/bold].")
        return
    console.print(dashboard_card(dash))
    if full:
        console.print(dashboard_allocation_table(dash))
        winners, losers = dashboard_movers(dash)
        console.print(winners)
        console.print(losers)
        rate = f"{dash.win_rate:.0f}%" if dash.win_rate is not None else "—"
        console.print(
            f"Win rate: [bold]{rate}[/bold] ({dash.positions_up} up / {dash.positions_down} down)"
        )
    if dash.xirr is not None:
        console.print(
            "[dim]XIRR uses recorded buy dates; snapshot-imported lots use estimated "
            "dates. vs S&P 500 (same-dates) is unaffected. terms: trd learn xirr · "
            "benchmark · concentration · total-return[/dim]"
        )


_SORT_KEYS = {
    "value": (lambda p: p.market_value or Decimal(0), True),
    "pl": (lambda p: p.unrealized_pl or Decimal(0), True),
    "pct": (lambda p: p.unrealized_pl_pct or Decimal(0), True),
    "day": (lambda p: p.day_change_pct or Decimal(0), True),
    "symbol": (lambda p: p.instrument.symbol, False),
}


@app.command()
def portfolio(
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Limit to one account.")
    ] = None,
    include_all: Annotated[
        bool, typer.Option("--all", help="Include simulation (paper) accounts.")
    ] = False,
    sort: Annotated[
        str, typer.Option("--sort", "-s", help="value | pl | pct | day | symbol.")
    ] = "value",
    min_value: Annotated[
        float, typer.Option("--min-value", help="Hide positions worth less than this.")
    ] = 0.0,
    as_json: JsonOpt = False,
) -> None:
    """Show holdings: weight, price + 30-day trend, value, day change, P&L.

    Sorted by value (biggest first); real money only unless --all.
    """
    _use_json(as_json)
    if sort not in _SORT_KEYS:
        err_console.print(f"[red]error:[/red] --sort must be one of {', '.join(_SORT_KEYS)}.")
        raise typer.Exit(code=1)
    service = _portfolio_service()
    try:
        with _spinner("Fetching quotes..."):
            positions = service.positions(account, include_simulation=include_all)
            sparks = service.sparklines(positions)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(positions)
        return
    if not positions:
        console.print("No open positions. Record one with [bold]trd buy[/bold].")
        return

    hidden = [p for p in positions if (p.market_value or Decimal(0)) < Decimal(str(min_value))]
    shown = [p for p in positions if p not in hidden]
    key, reverse = _SORT_KEYS[sort]
    shown.sort(key=key, reverse=reverse)

    value = sum((p.market_value for p in positions if p.market_value is not None), Decimal(0))
    cost = sum((p.cost_basis for p in positions), Decimal(0))
    day = sum((p.day_change for p in positions if p.day_change is not None), Decimal(0))
    pl = value - cost
    console.print(
        f"[bold]{fmt_money(value)}[/bold]  "
        f"today {fmt_signed(day)}  ·  P&L {fmt_signed(pl)} "
        f"({fmt_signed_pct(pl / cost * 100 if cost else None)})"
    )
    scope = account or ("all accounts" if include_all else "all real accounts")
    console.print(positions_table(shown, f"Portfolio — {scope}", sparklines=sparks))
    if hidden:
        dust = sum((p.market_value for p in hidden if p.market_value is not None), Decimal(0))
        console.print(
            f"[dim]+{len(hidden)} smaller position(s) hidden ({fmt_money(dust)}). "
            f"--min-value 0 to show all.[/dim]"
        )


@app.command()
def equity(
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Limit to one account.")
    ] = None,
    include_all: Annotated[
        bool, typer.Option("--all", help="Include simulation (paper) accounts.")
    ] = False,
    days: Annotated[
        int, typer.Option("--days", help="Look back this many days (0 = since first buy).")
    ] = 0,
    months: Annotated[
        int, typer.Option("--months", help="Look back this many months (overridden by --days).")
    ] = 0,
    daily: Annotated[
        bool,
        typer.Option("--daily", help="Show day-over-day P&L (contributions stripped) as a table."),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the curve as JSON.")] = False,
) -> None:
    """Portfolio value over time: equity curve, return, XIRR, and max drawdown.

    Computed from your transactions and stored price history — depth is bounded by
    how far back you've synced (use 'trd sync --years N' for a longer curve).
    """
    lookback = days if days > 0 else (months * 30 if months > 0 else None)
    service = EquityCurveService(connect(get_settings().db_path))
    try:
        curve = service.curve(account, lookback_days=lookback, include_simulation=include_all)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        console.print_json(curve.model_dump_json())
        return
    if daily:
        console.print(equity_daily_table(curve))
        return
    for renderable in equity_curve_renderables(curve):
        console.print(renderable)


@app.command()
def movers(
    sort: Annotated[str, typer.Option("--sort", "-s", help="day | pl | value | symbol.")] = "day",
    include_all: Annotated[
        bool, typer.Option("--all", help="Include simulation (paper) accounts.")
    ] = False,
) -> None:
    """What's up or down across everything you own and watch, ranked.

    Owned names carry P&L (today's $ + cumulative); watch-only names show price and
    the day move. Live quotes each run — no sync needed.
    """
    if sort not in ("day", "pl", "value", "symbol"):
        err_console.print("[red]error:[/red] --sort must be day, pl, value, or symbol.")
        raise typer.Exit(code=1)
    settings = get_settings()
    service = MoversService(connect(settings.db_path), YFinanceProvider())
    with _spinner("Fetching quotes..."):
        rows = service.board(sort=sort, include_simulation=include_all)
    if not rows:
        console.print("Nothing owned or watched yet. Buy or [bold]trd watch add[/bold] something.")
        return
    console.print(movers_table(rows, "Movers — owned + watched"))


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
    as_json: JsonOpt = False,
) -> None:
    """Per-purchase detail: buy date, price paid per share, total cost, gain since.

    Real money only by default — pass --all to include paper accounts.
    """
    _use_json(as_json)
    service = _portfolio_service()
    try:
        with _spinner("Fetching quotes..."):
            result = service.lots(account, symbol, include_simulation=include_all)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(result)
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
        with _spinner(f"Fetching {symbol.upper()}..."):
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
    as_json: JsonOpt = False,
) -> None:
    """Quote board: price, day change, 52-week range position, volume vs average."""
    _use_json(as_json)
    service = _watchlist_service()
    try:
        with _spinner("Fetching quotes..."):
            rows = service.board(list_name)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(rows)
        return
    if not rows:
        console.print("Nothing watched yet. Add with [bold]trd watch add SYMBOL[/bold].")
        return
    title = f"Watch — {list_name}" if list_name else "Watch — all lists"
    console.print(board_table(rows, title, show_list_column=list_name is None))


@exit_app.command("set")
def exit_set(
    symbol: Annotated[str, typer.Argument(help="Ticker the trigger applies to.")],
    account: AccountOpt = DEFAULT_ACCOUNT,
    stop: Annotated[
        str | None, typer.Option("--stop", help="Stop price: exit if a daily close is ≤ this.")
    ] = None,
    target: Annotated[
        str | None, typer.Option("--target", help="Target price: flag if a daily close is ≥ this.")
    ] = None,
    note: NoteOpt = None,
) -> None:
    """Set (or replace) a stop/target exit trigger on a holding. One per account+symbol."""
    service = _exit_service()
    stop_dec = _parse_decimal(stop, "stop") if stop is not None else None
    target_dec = _parse_decimal(target, "target") if target is not None else None
    try:
        service.set(symbol, account, stop=stop_dec, target=target_dec, note=note)
    except TrdError as exc:
        _fail(exc)
        return
    parts = []
    if stop_dec is not None:
        parts.append(f"stop {fmt_money(stop_dec)}")
    if target_dec is not None:
        parts.append(f"target {fmt_money(target_dec)}")
    console.print(
        f"Exit trigger set on [bold]{symbol.upper()}[/bold] ({account}): {', '.join(parts)}."
    )


@exit_app.command("rm")
def exit_rm(
    symbol: Annotated[str, typer.Argument(help="Ticker to clear the trigger for.")],
    account: AccountOpt = DEFAULT_ACCOUNT,
) -> None:
    """Remove a holding's exit trigger."""
    service = _exit_service()
    try:
        service.remove(symbol, account)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Removed exit trigger on [bold]{symbol.upper()}[/bold] ({account}).")


@exit_app.command("ls")
def exit_ls(
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Scope to one account. Omit for all.")
    ] = None,
) -> None:
    """List exit triggers vs the latest close (run 'trd sync' to refresh closes)."""
    service = _exit_service()
    try:
        rows = service.rows(account)
    except TrdError as exc:
        _fail(exc)
        return
    if not rows:
        console.print(
            "No exit triggers set. Add one with [bold]trd exit set SYMBOL --stop P[/bold]."
        )
        return
    title = f"Exit triggers — {account}" if account else "Exit triggers — all accounts"
    console.print(exit_table(rows, title, show_account=account is None))


@exit_app.command("check")
def exit_check(
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Scope to one account. Omit for all.")
    ] = None,
) -> None:
    """Flag triggers whose latest close has breached its stop or target."""
    service = _exit_service()
    try:
        rows = service.rows(account, breaches_only=True)
    except TrdError as exc:
        _fail(exc)
        return
    if not rows:
        console.print(
            "[green]✓ No exit triggers breached.[/green] All holdings within their levels."
        )
        return
    title = f"Exit triggers breached — {account}" if account else "Exit triggers breached"
    console.print(exit_table(rows, title, show_account=account is None))


@app.command()
def earnings(
    days: Annotated[int, typer.Option("--days", "-d", help="Look-ahead window in days.")] = 14,
    as_json: JsonOpt = False,
) -> None:
    """Upcoming earnings across portfolio and watchlists. Run 'trd sync' to refresh."""
    _use_json(as_json)
    settings = get_settings()
    service = EarningsService(connect(settings.db_path))
    events = service.upcoming(days)
    if as_json:
        _emit_json(events)
        return
    if not events:
        console.print(f"No earnings in the next {days} days. Run [bold]trd sync[/bold] to refresh.")
        return
    console.print(earnings_table(events, days))


def _write_prep_markdown(briefing: SundayPrepBriefing) -> Path:
    """Write the human-readable markdown to TRD_HOME/prep/ (synced via iCloud).
    The DB is the system of record; this file is for reading / a Claude session."""
    out_dir = get_settings().home / "prep"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{briefing.generated_for.isoformat()}.md"
    md_path.write_text(sunday_prep_markdown(briefing))
    return md_path


@app.command("sunday-prep")
@app.command("prep", hidden=True)  # short alias
def sunday_prep(
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit the briefing as structured JSON.")
    ] = False,
    snapshot: Annotated[
        bool,
        typer.Option("--snapshot", help="Persist to DuckDB + write markdown to TRD_HOME/prep/."),
    ] = False,
    history: Annotated[
        int,
        typer.Option(
            "--history",
            help="Show the last N saved briefings as a trend table instead of building one.",
        ),
    ] = 0,
    on: Annotated[
        str | None,
        typer.Option("--date", help="Reference date (ISO); defaults to today."),
    ] = None,
) -> None:
    """Sunday Prep: a structured week-ahead briefing — futures, macro events, earnings,
    sector leadership, volatility, key levels, themes, a watchlist, and risks."""
    if history > 0:
        rows = PrepHistoryService(connect(get_settings().db_path)).history(history)
        if not rows:
            console.print(
                "No saved briefings yet. Run [bold]trd sunday-prep --snapshot[/bold] first."
            )
            return
        console.print(prep_history_table(rows))
        return
    try:
        reference = date.fromisoformat(on) if on else date.today()
    except ValueError:
        _fail(TrdError(f"Bad --date {on!r}; use ISO format like 2026-06-14."))
        return
    service = SundayPrepService(YFinanceProvider())
    with _spinner("Building Sunday Prep — futures, sectors, earnings, levels..."):
        briefing = service.build(reference)
    if as_json:
        console.print_json(briefing.model_dump_json())
    else:
        for renderable in sunday_prep_renderables(briefing):
            console.print(renderable)
    if snapshot:
        PrepHistoryService(connect(get_settings().db_path)).save(briefing)
        path = _write_prep_markdown(briefing)
        console.print(f"[dim]Saved to DuckDB + {path}[/dim]")


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
        with _spinner("Fetching quotes..."):
            status = service.status(name)
    except TrdError as exc:
        _fail(exc)
        return
    _print_status(status, f"Plan — {name}")
    console.print("[dim]terms: trd learn pl · benchmark · dca[/dim]")


@plan_app.command("ls")
def plan_ls(
    pnl: Annotated[
        bool,
        typer.Option("--pnl", help="Show each plan's value, P&L, and vs-SPY (fetches quotes)."),
    ] = False,
) -> None:
    """All contribution plans (add --pnl for value / P&L / vs-SPY per plan)."""
    service = _plan_service()
    plans = service.list_plans()
    if not plans:
        console.print("No plans. Run [bold]trd plan set[/bold] or [bold]trd sim init[/bold].")
        return
    if pnl:
        with _spinner("Fetching quotes..."):
            statuses = [service.status(plan.account.name) for plan in plans]
        console.print(plans_pnl_table(statuses))
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
        with _spinner("Fetching quotes..."):
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
        with _spinner("Loading..."):
            detail = service.detail(name)
    except TrdError as exc:
        _fail(exc)
        return
    if not detail.events:
        console.print("No contributions yet. Run [bold]trd dca invest[/bold].")
        return
    console.print(dca_history_table(detail, limit))


def _projection_service() -> DcaProjectionService:
    settings = get_settings()
    return DcaProjectionService(connect(settings.db_path), YFinanceProvider())


@plan_app.command("forecast")
def plan_forecast(
    account: PlanAccountOpt = None,
    years: Annotated[int, typer.Option("--years", "-y", help="Projection horizon.")] = 10,
    monthly: Annotated[
        str | None, typer.Option("--monthly", "-m", help="Override the monthly amount.")
    ] = None,
    trials: Annotated[int, typer.Option("--trials", help="Monte Carlo trials.")] = 1000,
    seed: Annotated[
        int | None, typer.Option("--seed", help="Random seed (reproducible bands).")
    ] = None,
) -> None:
    """Project the plan forward from its own price history: expected path + bands."""
    service = _projection_service()
    try:
        name = account or service.plans.resolve_default_account()
        with _spinner("Simulating..."):
            result = service.forecast(
                name,
                years=years,
                monthly_override=float(_parse_decimal(monthly, "monthly")) if monthly else None,
                trials=trials,
                seed=seed,
            )
    except TrdError as exc:
        _fail(exc)
        return
    console.print(
        f"Based on [bold]{result.window_months}[/bold] monthly returns since "
        f"{result.window_start:%Y-%m} (window limited by {result.limiting_symbol}). "
        f"Historical CAGR of this mix: [bold]{result.cagr * 100:+.1f}%/yr[/bold]."
    )
    console.print(forecast_table(result))
    console.print(
        "[dim]Expected = steady growth at historical CAGR. Bands = "
        f"{result.trials} Monte Carlo resamples of your mix's actual months. "
        "Past performance doesn't promise the future — bands only show what your "
        "window contained. terms: trd learn cagr · monte-carlo · percentiles[/dim]"
    )


@plan_app.command("backtest")
def plan_backtest(
    account: PlanAccountOpt = None,
    years: Annotated[int, typer.Option("--years", "-y", help="How far back to replay.")] = 10,
) -> None:
    """Replay this exact plan against real history: what would have happened."""
    service = _projection_service()
    try:
        name = account or service.plans.resolve_default_account()
        with _spinner("Replaying history..."):
            result = service.backtest(name, years=years)
    except TrdError as exc:
        _fail(exc)
        return
    if result.window_limited_by:
        console.print(
            f"[yellow]Window shortened to {result.start} — "
            f"{result.window_limited_by} has no earlier history.[/yellow]"
        )
    console.print(backtest_table(result, name))
    console.print("[dim]terms: trd learn backtest · xirr · benchmark · adjusted-close[/dim]")


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
    alloc: AllocOpt = None,
) -> None:
    """Update an existing plan's amount, scheduled day, goal note, or allocation.

    --alloc re-targets the plan (weights sum to 100); recorded buys keep their
    symbols, only future contributions follow the new split.
    """
    service = _plan_service()
    try:
        name = account or service.resolve_default_account()
        plan = service.update_plan(
            name,
            monthly=_parse_decimal(monthly, "monthly amount") if monthly else None,
            day_of_month=day,
            note=note,
            allocations=_parse_allocs(alloc),
        )
    except TrdError as exc:
        _fail(exc)
        return
    day_text = f", day {plan.day_of_month}" if plan.day_of_month else ""
    console.print(f"Plan on [bold]{name}[/bold]: {fmt_money(plan.monthly_amount)}/month{day_text}.")
    if plan.allocations:
        legs = " / ".join(f"{w.normalize():f}% {s}" for s, w in plan.allocations.items())
        console.print(f"[dim]Allocation: {legs}[/dim]")


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
        with _spinner("Fetching quotes..."):
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


@app.command()
def backup(
    path: Annotated[Path, typer.Argument(help="Where to write the backup JSON.")],
) -> None:
    """Export user-owned data (accounts, transactions, plans, watchlists, indicators,
    exit triggers, engine state) to a portable JSON file. Prices/earnings are
    excluded — they rebuild with sync."""
    from trd.services.backup import export_data

    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        data = export_data(conn)
    finally:
        conn.close()
    path.write_text(_json.dumps(data, indent=2))
    engine = data.get("engine") or {}
    console.print(
        f"Backed up [bold]{len(data['transactions'])}[/bold] transactions, "
        f"[bold]{len(data['accounts'])}[/bold] accounts, "
        f"[bold]{len(data['plans'])}[/bold] plans, "
        f"{len(data['exit_triggers'])} exit triggers, "
        f"{len(engine.get('positions', []))} engine positions to {path}."
    )


@app.command()
def restore(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, help="Backup JSON to load.")],
    force: Annotated[
        bool, typer.Option("--force", help="Replace existing user data (destructive).")
    ] = False,
) -> None:
    """Rebuild a database from a backup, then run 'trd sync' to refresh prices."""
    import json as _json

    from trd.services.backup import restore_data

    settings = get_settings()
    db_path = settings.db_path
    if force and db_path.exists():
        # Rebuild from scratch — restore is insert-only into a fresh database.
        db_path.unlink()
        db_path.with_suffix(db_path.suffix + ".wal").unlink(missing_ok=True)
    conn = connect(db_path)
    try:
        stats = restore_data(conn, _json.loads(path.read_text()))
    except TrdError as exc:
        _fail(exc)
        return
    finally:
        conn.close()  # release the file so a later --force can recreate it
    console.print(
        f"Restored [bold]{stats.transactions}[/bold] transactions across "
        f"[bold]{stats.accounts}[/bold] accounts, {stats.plans} plans, "
        f"{stats.watchlists} watchlists, {stats.exit_triggers} exit triggers, "
        f"{stats.engine_positions} engine positions. "
        "Run [bold]trd sync[/bold] to refresh prices."
    )


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


@engine_app.command("init")
def engine_init(
    account: Annotated[
        str, typer.Option("--account", "-a", help="Simulation account the engine trades.")
    ] = DEFAULT_ENGINE_ACCOUNT,
    size: Annotated[str, typer.Option("--size", help="Dollars committed per trade.")] = "1000",
    max_positions: Annotated[int, typer.Option("--max", help="Most positions open at once.")] = 5,
    symbols: Annotated[
        str | None,
        typer.Option("--symbols", help="Comma-separated universe. Omit for the default ten."),
    ] = None,
    strategies: Annotated[
        str | None,
        typer.Option("--strategies", help="Comma-separated strategy keys. Omit for all."),
    ] = None,
    earnings_blackout: Annotated[
        int,
        typer.Option(
            "--earnings-blackout",
            help="Take no new entry within this many days of a print. 0 disables.",
        ),
    ] = DEFAULT_EARNINGS_BLACKOUT_DAYS,
    sizing: Annotated[
        str,
        typer.Option(
            "--sizing",
            help="'exposure' commits --size per trade; 'risk' risks --size per trade.",
        ),
    ] = "exposure",
    flat_at: Annotated[
        int,
        typer.Option(
            "--flat-at",
            help="Day mode: flatten at this time (HHMM, e.g. 1555). 0 = carry overnight.",
        ),
    ] = 0,
) -> None:
    """Set up the engine: a simulation account, a 10-name universe, and the rule set."""
    service = _engine_service()
    try:
        mode = SizingMode(sizing)
    except ValueError:
        err_console.print(f"[red]error:[/red] --sizing must be exposure or risk, not {sizing!r}")
        raise typer.Exit(code=1) from None
    try:
        config, acct, universe = service.init(
            earnings_blackout_days=earnings_blackout,
            account_name=account,
            position_size=_parse_decimal(size, "size"),
            max_positions=max_positions,
            symbols=[s.strip().upper() for s in symbols.split(",") if s.strip()]
            if symbols
            else None,
            strategies=[s.strip() for s in strategies.split(",") if s.strip()]
            if strategies
            else None,
            exit_params={"flat_at_minute": float(flat_at)} if flat_at else None,
            sizing_mode=mode,
        )
    except TrdError as exc:
        _fail(exc)
        return
    flat = int(config.exit_params.get("flat_at_minute", 0))
    console.print(
        f"Engine ready on [bold]{acct.name}[/bold] (simulation).\n"
        f"Universe ({len(universe)}): {', '.join(universe)}\n"
        f"Strategies: {', '.join(config.strategies)}\n"
        + (
            f"Sizing: {fmt_money(config.position_size)} "
            + ("at risk" if config.sizing_mode == SizingMode.RISK else "committed")
            + f" per trade, max {config.max_positions} open.\n"
        )
        + (
            f"Day mode: flat at {flat // 100:02d}:{flat % 100:02d}, "
            f"no new entries after {(flat // 100 * 60 + flat % 100 - 30) // 60:02d}:"
            f"{(flat // 100 * 60 + flat % 100 - 30) % 60:02d}.\n"
            if flat
            else "Swing mode: positions carry overnight.\n"
        )
        + (
            f"No new entry within {config.earnings_blackout_days}d of earnings.\n\n"
            if config.earnings_blackout_days
            else "Earnings blackout off.\n\n"
        )
        + "Next: [bold]trd sync --full[/bold] (the rules need 200 bars of history), "
        "then [bold]trd engine scan[/bold]."
    )


def _emit_scan(result: ScanResult, as_json: bool, ndjson: bool) -> None:
    """Render one scan in whichever shape the consumer wants: a human at a
    terminal, a log shipper, or a one-shot JSON blob."""
    if ndjson:
        # One event per line, unindented — this is what promtail tails.
        for event in scan_events(result):
            print(_json.dumps(event, separators=(",", ":")))
        return
    if as_json:
        console.print_json(result.model_dump_json())
        return
    for renderable in engine_scan_renderables(result):
        console.print(renderable)


def _notify_scan(result: ScanResult, label: str | None = None) -> None:
    """Push fills to a chat, if one is configured. A delivery failure is reported
    and swallowed: the trades are already recorded, and failing the scan over an
    undelivered message would make the next pass re-evaluate a stale world."""
    messages = scan_messages(result, label)
    if not messages:
        return
    notifier = notify_from_env()
    if notifier is None:
        err_console.print(
            "[yellow]warning:[/yellow] --notify set but TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID are not configured — nothing sent."
        )
        return
    for message in messages:
        try:
            notifier.send(message)
        except TrdError as exc:
            err_console.print(f"[yellow]warning:[/yellow] notification failed: {exc}")


@engine_app.command("scan")
def engine_scan(
    paper: Annotated[
        bool, typer.Option("--paper/--no-paper", help="Take paper fills, or only log signals.")
    ] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the scan result as JSON.")] = False,
    ndjson: Annotated[
        bool,
        typer.Option("--ndjson", help="Emit one JSON event per line (for log shipping)."),
    ] = False,
    notify: Annotated[
        bool, typer.Option("--notify", help="Push fills to the configured chat (Telegram).")
    ] = False,
) -> None:
    """Run one scan pass: manage exits, then take the best new entries."""
    try:
        quotes = _prefetched_quotes(lambda s: s.quote_symbols())
        service = _engine_service()
        result = service.scan(paper=paper, quotes=quotes)
    except TrdError as exc:
        _fail(exc)
        return
    _emit_scan(result, as_json, ndjson)
    if notify:
        _notify_scan(result, label_from_env(service.config().exit_params))


@engine_app.command("monitor")
def engine_monitor(
    interval: Annotated[int, typer.Option("--interval", "-i", help="Seconds between scans.")] = 60,
    paper: Annotated[
        bool, typer.Option("--paper/--no-paper", help="Take paper fills, or only log signals.")
    ] = True,
    passes: Annotated[
        int | None, typer.Option("--passes", help="Stop after N scans. Omit to run until Ctrl-C.")
    ] = None,
    ndjson: Annotated[
        bool,
        typer.Option("--ndjson", help="Emit one JSON event per line (for log shipping)."),
    ] = False,
    notify: Annotated[
        bool, typer.Option("--notify", help="Push fills to the configured chat (Telegram).")
    ] = False,
) -> None:
    """Scan on a loop. Ctrl-C to stop — every pass is already persisted."""
    import time

    count = 0
    if not ndjson:
        console.print(f"[dim]Monitoring every {interval}s. Ctrl-C to stop.[/dim]")
    try:
        while passes is None or count < passes:
            # Connect per pass, not once for the whole loop: a monitor that held
            # the connection would keep DuckDB's single writer lock for its entire
            # run, blocking every other trd command for as long as it watches.
            try:
                quotes = _prefetched_quotes(lambda s: s.quote_symbols())
                service = _engine_service()
                result = service.scan(paper=paper, quotes=quotes)
                params = service.config().exit_params
                service.conn.close()
            except TrdError as exc:
                _fail(exc)
                return
            _emit_scan(result, as_json=False, ndjson=ndjson)
            if notify:
                _notify_scan(result, label_from_env(params))
            count += 1
            if passes is not None and count >= passes:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        console.print(f"\n[dim]Stopped after {count} scan(s).[/dim]")


@engine_app.command("signals")
def engine_signals(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to show.")] = 25,
    strategy: Annotated[
        str | None, typer.Option("--strategy", "-s", help="Filter to one strategy key.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Signal history — everything the rules saw, taken or passed over."""
    _use_json(as_json)
    service = _engine_service()
    try:
        rows = service.signal_rows(limit=limit, strategy=strategy)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(rows)
        return
    if not rows:
        console.print("No signals yet. Run [bold]trd engine scan[/bold].")
        return
    console.print(engine_signals_table(rows))


@engine_app.command("positions")
def engine_positions(
    all_: Annotated[
        bool, typer.Option("--all", help="Include closed trades, not just open ones.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Engine positions with live P&L and where the stops sit."""
    _use_json(as_json)
    try:
        quotes = _prefetched_quotes(lambda s: s.quote_symbols())
        service = _engine_service()
        rows = service.position_rows(open_only=not all_, quotes=quotes)
        max_positions = service.config().max_positions
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(rows)
        return
    if not rows:
        console.print("No engine positions yet. Run [bold]trd engine scan[/bold].")
        return
    title = "Engine positions — all" if all_ else "Engine positions — open"
    console.print(engine_positions_table(rows, title, show_exit=all_, max_positions=max_positions))


@engine_app.command("report")
def engine_report(as_json: JsonOpt = False) -> None:
    """Per-strategy scorecard over closed trades: win rate, avg win/loss, expectancy."""
    _use_json(as_json)
    service = _engine_service()
    try:
        stats = service.report()
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(stats)
        return
    if not stats:
        console.print("No trades yet. Run [bold]trd engine scan[/bold] for a while first.")
        return
    console.print(engine_report_table(stats))


@engine_app.command("runs")
def engine_runs(
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many scans to show.")] = 25,
    today: Annotated[bool, typer.Option("--today", help="Every scan since midnight.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Scan history with the interval between passes. The gaps are the point: a
    CronJob that stopped, or a scan that never fired, is invisible everywhere
    else — 'the engine did nothing' and 'the engine never ran' look identical
    until you can see the cadence."""
    _use_json(as_json)
    service = _engine_service()
    try:
        runs = service.run_rows(limit=limit, today=today)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(runs)
        return
    if not runs:
        console.print("No scans recorded yet. Run [bold]trd engine scan[/bold].")
        return
    console.print(engine_runs_table(runs))


@engine_app.command("why")
def engine_why(
    symbol: Annotated[str, typer.Argument(help="A symbol you currently hold.")],
    as_json: JsonOpt = False,
) -> None:
    """Why this trade was taken, what the indicators mean, and how it ends.

    The reason is the one recorded when the signal fired, so it shows what the
    rule actually saw that day rather than what it would say about today.
    """
    _use_json(as_json)
    service = _engine_service()
    try:
        why = service.explain(symbol)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(why)
        return
    for renderable in engine_why_renderables(why):
        console.print(renderable)


@engine_app.command("status")
def engine_status(
    as_json: JsonOpt = False,
) -> None:
    """What this engine is and whether it is healthy: build, database, rule set,
    capacity, data depth, and whether it is actually running. No network call —
    it answers even when the data provider is the thing that is broken."""
    _use_json(as_json)
    settings = get_settings()
    service = _engine_service()
    try:
        status = service.status(db_path=str(settings.db_path))
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(status)
        return
    for renderable in engine_status_renderables(status):
        console.print(renderable)


@engine_app.command("backtest")
def engine_backtest(
    years: Annotated[
        int | None, typer.Option("--years", help="Replay the last N years. Omit for everything.")
    ] = None,
    start: Annotated[
        str | None, typer.Option("--start", help="First trading date (ISO). Earlier bars warm up.")
    ] = None,
    end: Annotated[str | None, typer.Option("--end", help="Last date to replay (ISO).")] = None,
    fill: Annotated[
        str,
        typer.Option(
            "--fill",
            help="Exit fill model: 'intrabar' checks stops/targets against each bar's "
            "range (gaps fill at the open); 'close' only ever fills at the close.",
        ),
    ] = "intrabar",
    blackout: Annotated[
        bool,
        typer.Option(
            "--blackout/--no-blackout",
            help="Honour the earnings blackout, or switch it off to compare.",
        ),
    ] = True,
    symbols: Annotated[
        str | None,
        typer.Option(
            "--symbols",
            help="Comma-separated universe override — vet a candidate name against "
            "history without touching the live watchlist.",
        ),
    ] = None,
    sizing: Annotated[
        str | None,
        typer.Option("--sizing", help="Override sizing for this run: exposure | risk."),
    ] = None,
    capital: Annotated[
        str | None,
        typer.Option("--capital", help="Starting cash. Required to compare sizing modes fairly."),
    ] = None,
    size: Annotated[
        str | None,
        typer.Option(
            "--size", help="Override dollars per trade — needed to compare sizing modes fairly."
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the result as JSON.")] = False,
) -> None:
    """Replay the engine's rules against stored history. Same rules, same
    scorecard as 'trd engine report' — hundreds of trades in one run instead of
    four years of waiting. Needs depth: run 'trd sync --years 10' first."""
    settings = get_settings()
    service = BacktestService(connect(settings.db_path))
    try:
        fill_mode = FillMode(fill)
    except ValueError:
        err_console.print(f"[red]error:[/red] --fill must be 'intrabar' or 'close', not {fill!r}")
        raise typer.Exit(code=1) from None
    start_at = _parse_date(start)
    end_at = _parse_date(end)
    try:
        result = service.run(
            years=years,
            start=start_at.date() if start_at else None,
            end=end_at.date() if end_at else None,
            fill=fill_mode,
            blackout=blackout,
            symbols=[s for s in symbols.split(",")] if symbols else None,
            sizing_mode=SizingMode(sizing) if sizing else None,
            position_size=_parse_decimal(size, "size") if size else None,
            capital=_parse_decimal(capital, "capital") if capital else None,
        )
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        console.print_json(result.model_dump_json())
        return
    for renderable in engine_backtest_renderables(result):
        console.print(renderable)


@app.command("history")
def history(
    days: Annotated[int, typer.Option("--days", "-d", help="Look back this many days.")] = 30,
    all_time: Annotated[bool, typer.Option("--all-time", help="Ignore the window.")] = False,
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Limit to one account.")
    ] = None,
    symbol: Annotated[
        str | None, typer.Option("--symbol", "-s", help="Limit to one ticker.")
    ] = None,
    side: Annotated[str | None, typer.Option("--side", help="buy or sell.")] = None,
    include_all: Annotated[
        bool, typer.Option("--all", help="Include simulation (paper) accounts.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """What you bought and sold, newest first, and whether the period made money.

    Every sell carries its realized P&L, matched against the lots it consumed.
    Real money only unless --all: the engine makes several paper fills a day and
    would bury the trades worth reviewing.
    """
    _use_json(as_json)
    parsed_side = None
    if side is not None:
        try:
            parsed_side = Side(side.lower())
        except ValueError:
            err_console.print(f"[red]error:[/red] --side must be buy or sell, not {side!r}")
            raise typer.Exit(code=1) from None
    settings = get_settings()
    service = HistoryService(connect(settings.db_path))
    try:
        result = service.history(
            days=None if all_time else days,
            account=account,
            symbol=symbol,
            side=parsed_side,
            include_simulation=include_all,
        )
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(result)
        return
    if not result.rows:
        if result.latest_outside_window is not None:
            # There IS history, just not here. Saying only "none in 30 days" reads
            # as an empty database and sends people looking for a bug.
            ago = (date.today() - result.latest_outside_window).days
            console.print(
                f"No transactions in the last {days} days — the most recent was "
                f"[bold]{result.latest_outside_window}[/bold] ({ago} days ago).\n"
                f"Try [bold]trd history --days {max(60, ago + 15)}[/bold] "
                "or [bold]trd history --all-time[/bold]."
            )
            return
        console.print("No transactions recorded. Add one with [bold]trd buy[/bold].")
        return
    console.print(history_table(result))


@app.command("version")
def version_cmd() -> None:
    """Which build this is: package version plus the git SHA baked in at image
    build. Answers 'is the deployed engine running the code I think it is'."""
    console.print(build_version())


@engine_app.command("rules")
def engine_rules() -> None:
    """What every entry strategy looks for and how each exit rule works."""
    service = _engine_service()
    params = None
    with suppress(TrdError):  # rules are readable before the engine is configured
        params = service.config().exit_params
    console.print(engine_strategies_table())
    console.print(engine_exits_table(params))


def main() -> None:
    """Console entry point. Renders any TrdError that escapes a command (e.g. a
    database lock raised while opening the connection, before the command's own
    try/except) as a clean one-line error instead of a traceback."""
    try:
        app()
    except TrdError as exc:
        # argv only, never the flag a command sets: main() is the process
        # entrypoint, so argv is authoritative and cannot carry state from a
        # previous in-process invocation.
        if _json_requested():
            print(
                _json.dumps(
                    {"error": type(exc).__name__, "message": str(exc)}, separators=(",", ":")
                )
            )
        else:
            err_console.print(f"[red]error:[/red] {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
