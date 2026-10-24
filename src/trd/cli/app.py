import json as _json
import os
import sys
from contextlib import nullcontext, suppress
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any

import duckdb
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.live import Live
from rich.table import Table

from trd.build import build_version
from trd.cli.render import (
    agenda_renderables,
    ai_review_renderables,
    alloc_view_renderables,
    backtest_table,
    board_table,
    cash_flows_table,
    dashboard_allocation_table,
    dashboard_card,
    dashboard_movers,
    dca_cadence_table,
    dca_history_table,
    dca_summary_table,
    dca_symbols_table,
    earnings_table,
    edge_table,
    engine_backtest_renderables,
    engine_cash_renderables,
    engine_exits_table,
    engine_guard_renderables,
    engine_intents_table,
    engine_monitor_view,
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
    exit_whatif_renderables,
    fidelity_renderables,
    fill_ingest_renderables,
    fmt_money,
    fmt_signed,
    fmt_signed_pct,
    forecast_table,
    history_table,
    income_table,
    indicator_panel,
    lots_table,
    movers_table,
    outbox_renderables,
    outcome_summary_renderables,
    outcome_trades_table,
    plans_pnl_table,
    positions_table,
    prep_history_table,
    proposal_renderables,
    reconcile_renderables,
    review_renderables,
    sunday_prep_markdown,
    sunday_prep_renderables,
    sweep_renderables,
)
from trd.config import DEFAULT_ACCOUNT, get_settings
from trd.db.connection import connect, connect_read_only
from trd.engine.bars import DAILY
from trd.errors import TrdError
from trd.models import (
    AccountType,
    BrokerSnapshot,
    EngineMode,
    FillSnapshot,
    IncomeKind,
    Side,
    SizingMode,
)
from trd.models.alloc import AllocTarget, CashFlowKind, CashYield
from trd.notify import alert_notifier_from_env, label_from_env, scan_alert_messages, scan_messages
from trd.notify.bot import (
    POLL_TIMEOUT,
    BotConfigError,
    bot_from_env,
    engines_from_env,
)
from trd.notify.messages import (
    alloc_report_message,
    approval_message,
    daily_report_message,
    fill_alert_message,
    fill_messages,
    halt_message,
    live_report_message,
    review_message,
    strip_html,
)
from trd.notify.telegram import LIVE_LABEL, TelegramNotifier
from trd.notify.telegram import from_env as notify_from_env
from trd.providers import YFinanceProvider
from trd.repos import AccountRepo
from trd.repos.review_snapshot import ReviewSnapshotRepo, ReviewSnapshotRow
from trd.services import (
    DashboardService,
    DcaDetailService,
    DcaProjectionService,
    EarningsService,
    EngineService,
    EquityCurveService,
    ExitTriggerService,
    FillIngestService,
    HistoryService,
    IncomeService,
    IndicatorService,
    MoversService,
    OutboxService,
    PlanService,
    PortfolioService,
    PrepHistoryService,
    ReconcileService,
    SundayPrepBriefing,
    SundayPrepService,
    SyncService,
    WatchlistService,
)
from trd.services.agenda import (
    DEFAULT_MIN_SESSIONS,
    DEFAULT_WINDOW_SESSIONS,
    agenda,
)
from trd.services.alloc import AllocService, CashService, resolve_cash_yield
from trd.services.alloc_report import AllocReportService
from trd.services.backtest import BacktestService, EntryFill, FillMode
from trd.services.commands import CommandQueueService
from trd.services.daily_report import (
    DEFAULT_LIVE_TWIN,
    DEFAULT_WINDOW_DAYS,
    DailyReport,
    EngineDay,
    engine_day,
    failed_engine,
)
from trd.services.edge import DEFAULT_SAMPLES as EDGE_SAMPLES
from trd.services.edge import DEFAULT_SEED as EDGE_SEED
from trd.services.edge import EdgeService
from trd.services.engine import (
    DEFAULT_EARNINGS_BLACKOUT_DAYS,
    DEFAULT_ENGINE_ACCOUNT,
    ScanResult,
    scan_events,
)
from trd.services.exit_whatif import ExitWhatIfService, parse_variants
from trd.services.fidelity import FidelityService, window_start
from trd.services.indicators import seed_defaults
from trd.services.live_report import live_report
from trd.services.outcomes import OutcomeService
from trd.services.plan import PlanStatus
from trd.services.review import (
    DEFAULT_REVIEW_WINDOW,
    EnginePack,
    ReviewPack,
    ReviewResult,
    engine_pack,
    review,
)
from trd.services.sweep import parse_axes, sweep
from trd.services.watchdog import (
    DEFAULT_MAX_AGE_MINUTES as WATCHDOG_MAX_AGE,
)
from trd.services.watchdog import (
    DEFAULT_REPEAT_MINUTES as WATCHDOG_REPEAT,
)
from trd.services.watchdog import (
    STATE_FILENAME as WATCHDOG_STATE_FILENAME,
)
from trd.services.watchdog import (
    WatchdogState,
)
from trd.services.watchdog import (
    check as watchdog_check,
)
from trd.services.watchdog import (
    decide as watchdog_decide,
)
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
income_app = typer.Typer(
    help="Cash your holdings paid you: dividends, interest, a broker's cash sweep.",
    no_args_is_help=True,
)
app.add_typer(income_app, name="income")
engine_app = typer.Typer(
    help="Monitor-mode trading engine: scan a universe, paper-trade the signals.",
    no_args_is_help=True,
)
app.add_typer(engine_app, name="engine")
bot_app = typer.Typer(
    help="Telegram command bot: drive the engines from chat.", no_args_is_help=True
)
app.add_typer(bot_app, name="bot")
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


def _reconcile_service() -> ReconcileService:
    """No provider: reconciliation is arithmetic over a snapshot file and the
    database, and must stay answerable when the market data source is down."""
    settings = get_settings()
    return ReconcileService(connect(settings.db_path))


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
    earnings_only: Annotated[
        bool,
        typer.Option(
            "--earnings-only",
            help="Refresh only earnings dates (no quotes, no bars) — cheap enough "
            "to run every scan, because some dates publish mid-session.",
        ),
    ] = False,
    intraday_only: Annotated[
        bool,
        typer.Option(
            "--intraday-only",
            help="Refresh only the intraday bars an intraday engine reads — cheap "
            "enough to run every scan, because its bars settle every few minutes.",
        ),
    ] = False,
    require_current: Annotated[
        bool,
        typer.Option(
            "--require-current",
            help="Exit non-zero if any symbol is left behind the newest bar this "
            "sync stored — lets a caller retry instead of stamping the day done.",
        ),
    ] = False,
    require_settled: Annotated[
        bool,
        typer.Option(
            "--require-settled",
            help="AFTER THE CLOSE: exit non-zero unless every symbol's bar for today "
            "matches its closing quote (implies --require-current). The evening "
            "close sync uses it, so a morning bar is never stamped as the close.",
        ),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the result as JSON.")] = False,
) -> None:
    """Refresh quotes and daily price history for all tracked instruments."""
    settings = get_settings()
    conn = connect(settings.db_path)
    service = SyncService(conn, YFinanceProvider())
    if intraday_only:
        with _spinner("Refreshing intraday bars..."):
            failures: list[str] = []
            count, timeframe = service.sync_intraday(failures)
        if timeframe is None:
            console.print("No intraday engine configured — nothing to refresh.")
        else:
            console.print(f"Synced [bold]{count}[/bold] {timeframe} bars for the engine universe.")
        if failures:
            err_console.print(f"[yellow]warning:[/yellow] failed: {', '.join(failures)}")
        return
    if earnings_only:
        with _spinner("Refreshing earnings dates..."):
            earnings = service.sync_earnings()
        console.print(
            f"Checked [bold]{earnings.checked}[/bold] symbols, "
            f"[bold]{earnings.events}[/bold] earnings dates."
        )
        if earnings.failures:
            err_console.print(f"[yellow]warning:[/yellow] failed: {', '.join(earnings.failures)}")
        return
    _use_json(as_json)
    with _spinner("Syncing market data..."):
        result = service.sync(full=full, years=years, settle=require_settled)
    if as_json:
        _emit_json(result)
    else:
        console.print(
            f"Synced [bold]{result.quotes}[/bold]/{result.instruments} quotes, "
            f"[bold]{result.bars}[/bold] daily bars, "
            f"[bold]{result.earnings}[/bold] earnings dates."
            + (
                f"\nPlus [bold]{result.intraday_bars}[/bold] {result.intraday_timeframe} bars "
                "for the engine universe."
                if result.intraday_timeframe
                else ""
            )
        )
        if result.failures:
            err_console.print(f"[yellow]warning:[/yellow] failed: {', '.join(result.failures)}")
        # Named separately from `failures` because it is a different fault: the
        # provider answered, said nothing, and left the name priced at an older
        # close. Every mark derived from it — unrealized, risk at stop, R — is
        # reading a stale number while looking entirely healthy.
        if result.stale_symbols:
            err_console.print(
                f"[yellow]warning:[/yellow] no current daily bar for "
                f"{', '.join(result.stale_symbols)} — marks for these are stale. "
                "Re-run 'trd sync' once the provider has published them."
            )
        # Reported, never blocking. These cannot catch up, so counting them as
        # stale would fail --require-current on every pass forever.
        if result.dormant_symbols:
            err_console.print(
                f"[yellow]note:[/yellow] {', '.join(result.dormant_symbols)} "
                f"stopped printing several sessions ago — halted, delisted or "
                "renamed. Marks are frozen; consider removing them."
            )
        # Deliberately after the report: the caller wanting to retry still gets to see
        # what happened, and a partial sync is worth keeping either way.
        if result.unsettled_symbols:
            err_console.print(
                f"[yellow]warning:[/yellow] today's bar is not the close yet for "
                f"{', '.join(result.unsettled_symbols)} — the stored close differs "
                "from the closing quote. Re-run 'trd sync' shortly."
            )
    if (require_current or require_settled) and result.stale_symbols:
        raise typer.Exit(code=1)
    if require_settled and result.unsettled_symbols:
        raise typer.Exit(code=1)


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
    table.add_row("Industry", info.industry or "—")
    table.add_row("Country", info.country or "—")
    table.add_row("Currency", info.currency)
    console.print(table)
    if info.summary:
        console.print(info.summary, style="dim")


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


@income_app.command("add")
def income_add(
    amount: Annotated[str, typer.Argument(help="Cash received, e.g. 1.98.")],
    symbol: Annotated[
        str | None,
        typer.Option("--symbol", "-s", help="The holding that paid it. Required for a dividend."),
    ] = None,
    account: Annotated[
        str, typer.Option("--account", "-a", help="Account the cash landed in.")
    ] = DEFAULT_ACCOUNT,
    date_str: Annotated[
        str | None, typer.Option("--date", "-d", help="Date received (YYYY-MM-DD). Default: today.")
    ] = None,
    kind: Annotated[
        str,
        typer.Option("--kind", "-k", help="dividend | interest | cash_sweep."),
    ] = "dividend",
    note: Annotated[str | None, typer.Option("--note", help="Anything worth remembering.")] = None,
) -> None:
    """Record cash a holding paid you.

    A dividend is not a buy: it creates no shares and changes no cost basis, so
    it never reaches the FIFO arithmetic. What it does change is every return
    figure — until it is recorded, trd measures price appreciation alone and
    reports less than you actually made.

    A REINVESTED dividend is a different thing. It buys shares at a real price
    and belongs in 'trd buy', not here; recording it in both places counts it
    twice.
    """
    try:
        kind_value = IncomeKind(kind)
    except ValueError:
        _fail(TrdError(f"Unknown kind '{kind}'. Use: {', '.join(k.value for k in IncomeKind)}."))
        return
    received = _parse_date(date_str) or datetime.now()
    settings = get_settings()
    service = IncomeService(connect(settings.db_path), YFinanceProvider())
    try:
        entry = service.add(
            account_name=account,
            amount=_parse_decimal(amount, "amount"),
            received_at=received,
            symbol=symbol,
            kind=kind_value,
            note=note,
        )
    except TrdError as exc:
        _fail(exc)
        return
    paid_by = f" from {symbol.upper()}" if symbol else ""
    console.print(
        f"Recorded {entry.kind.value.replace('_', ' ')} {fmt_money(entry.amount)}{paid_by} "
        f"in [bold]{account}[/bold] on {entry.received_at:%Y-%m-%d}."
    )


@income_app.command("ls")
def income_ls(
    account: Annotated[
        str | None, typer.Option("--account", "-a", help="Only this account.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Every payment received, oldest first, with the running total."""
    _use_json(as_json)
    settings = get_settings()
    service = IncomeService(connect(settings.db_path), YFinanceProvider())
    try:
        rows = service.ledger(account)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(
            [
                {**entry.model_dump(mode="json"), "symbol": inst.symbol if inst else None}
                for entry, inst in rows
            ]
        )
        return
    if not rows:
        console.print(
            "No income recorded. A dividend your broker paid is cash trd cannot see "
            "until you add it: [bold]trd income add 1.98 --symbol VOO[/bold]."
        )
        return
    console.print(income_table(rows))
    console.print("[dim]terms: trd learn income · total-return · price-return[/dim]")


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
        str,
        typer.Option(
            "--account",
            "-a",
            help="Account the engine trades: simulation for paper, real with --live.",
        ),
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
    timeframe: Annotated[
        str,
        typer.Option(
            "--timeframe",
            help="Bar width the rules run on: 1d (swing) or 5m/15m/30m/1h (day).",
        ),
    ] = DAILY,
    max_entries_per_day: Annotated[
        int,
        typer.Option(
            "--max-entries-per-day",
            help=(
                "Most new entries in one session. 0 = unlimited. Caps the flow of "
                "trades, where --max caps how many are open at once."
            ),
        ),
    ] = 0,
    regime_sma: Annotated[
        int,
        typer.Option(
            "--regime-sma",
            help="Block new entries while SPY closes below its N-day average. 0 = off.",
        ),
    ] = 0,
    regime_vix_max: Annotated[
        float,
        typer.Option(
            "--regime-vix-max",
            help="Block new entries while VIX closes above this level. 0 = off.",
        ),
    ] = 0.0,
    scale_out_pct: Annotated[
        float,
        typer.Option(
            "--scale-out",
            help=(
                "Percent of the position to sell at the target, leaving the rest to "
                "ride the trail. 0 = off (close flat at the target)."
            ),
        ),
    ] = 0.0,
    capital: Annotated[
        str | None,
        typer.Option(
            "--capital",
            help=(
                "Cash the engine trades with. Turns on the T+1 settled-cash ledger: "
                "an entry needs settled cash, and sale proceeds wait a business day. "
                "Omit for no ledger."
            ),
        ),
    ] = None,
    live: Annotated[
        bool,
        typer.Option(
            "--live",
            help=(
                "Trade a REAL account: scans record order intents instead of filling. "
                "Needs --capital, daily bars and no --flat-at."
            ),
        ),
    ] = False,
) -> None:
    """Set up the engine: an account, a 10-name universe, and the rule set.

    Paper (the default) fills its own trades in a simulation account. --live
    trades a real account and writes order intents ('trd engine intents').
    """
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
            exit_params={
                k: v
                for k, v in {
                    "flat_at_minute": float(flat_at),
                    "regime_sma": float(regime_sma),
                    "regime_vix_max": float(regime_vix_max),
                    "scale_out_pct": float(scale_out_pct),
                }.items()
                if v
            }
            or None,
            sizing_mode=mode,
            timeframe=timeframe,
            max_entries_per_day=max_entries_per_day,
            capital=_parse_decimal(capital, "capital") if capital else None,
            mode=EngineMode.LIVE if live else EngineMode.PAPER,
        )
        # SPY and ^VIX become tracked instruments so `trd sync` pulls their bars.
        # Registered unconditionally: the backtest needs them to answer "what
        # would the gate have done", which is the question you ask *before*
        # switching it on.
        service.ensure_regime_instruments()
    except TrdError as exc:
        _fail(exc)
        return
    flat = int(config.exit_params.get("flat_at_minute", 0))
    console.print(
        f"Engine ready on [bold]{acct.name}[/bold] "
        + (
            "([bold red]LIVE[/bold red] — real account; scans write order intents, "
            "'trd engine intents').\n"
            if config.is_live
            else "(simulation).\n"
        )
        + f"Universe ({len(universe)}): {', '.join(universe)}\n"
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
        + f"Timeframe: {config.timeframe} bars.\n"
        + (
            "Next: [bold]trd sync[/bold] (it pulls the intraday bars this engine needs), "
            "then [bold]trd engine scan[/bold]."
            if config.is_intraday
            else "Next: [bold]trd sync --full[/bold] (the rules need 200 bars of history), "
            "then [bold]trd engine scan[/bold]."
        )
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


LIVE_INTENT_ALERTS_ENV = "TRD_LIVE_INTENT_ALERTS"


def _notify_scan(result: ScanResult, label: str | None = None) -> None:
    """Push fills to a chat, if one is configured. A delivery failure is reported
    and swallowed: the trades are already recorded, and failing the scan over an
    undelivered message would make the next pass re-evaluate a stale world.

    A live scan's order intents go to the alert chat instead: each asks a person
    to do something, and the channel is a feed of things already done — but only
    with TRD_LIVE_INTENT_ALERTS=1. With the live runner draining the outbox
    (SB-1382) the person's one message per order is the approval request
    `outbox ack --notify` sends minutes later; "waiting for the outbox" before it
    is the same order twice. Set it when the outbox is drained by hand."""
    _send(scan_messages(result, label))
    if os.environ.get(LIVE_INTENT_ALERTS_ENV, "").strip() == "1":
        _send(scan_alert_messages(result, label), alert=True)
    if result.auto_halt:
        _send([halt_message(result.auto_halt, label)], alert=True)


def _send(messages: list[str], alert: bool = False) -> None:
    """Deliver to the channel, or with `alert` to the live alert chat. Missing
    configuration and delivery failures are warnings, never errors: whatever the
    message reports has already been recorded."""
    if not messages:
        return
    warn = lambda text: err_console.print(f"[yellow]warning:[/yellow] {text}")  # noqa: E731
    refused: list[str] = []

    def error(text: str) -> None:
        refused.append(text)
        err_console.print(f"[red]error:[/red] {text}")

    notifier = alert_notifier_from_env(warn=warn, error=error) if alert else notify_from_env()
    if notifier is None:
        if refused:
            return  # already said why, and it is not a missing token
        warn(
            "--notify set but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not configured "
            "— nothing sent."
        )
        return
    for message in messages:
        try:
            notifier.send(message)
        except TrdError as exc:
            warn(f"notification failed: {exc}")


@engine_app.command("add")
def engine_add(
    symbol: Annotated[str, typer.Argument(help="Ticker to add to this engine's universe.")],
    as_json: JsonOpt = False,
) -> None:
    """Add a symbol to this engine's universe and pull the history its rules need.

    The same code path the Telegram bot's /add drains, so chat and terminal cannot
    give different answers. Backfills only this symbol — `trd sync --full` would
    re-pull two years for the whole universe to serve one addition — and reports
    whether it clears the engine's warmup, because a name in the universe with no
    history is skipped every pass and reads as a broken engine rather than one
    warming up.
    """
    _use_json(as_json)
    settings = get_settings()
    try:
        service = CommandQueueService(connect(settings.db_path), YFinanceProvider())
        with _spinner(f"Adding {symbol.upper()} and pulling its history..."):
            message = service.add(symbol)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json({"symbol": symbol.upper(), "message": message})
    else:
        console.print(message)


@engine_app.command("rm")
def engine_rm(
    symbol: Annotated[str, typer.Argument(help="Ticker to drop from this engine's universe.")],
    as_json: JsonOpt = False,
) -> None:
    """Drop a symbol from this engine's universe.

    Deliberately does not touch an open position in that name: dropping it stops
    new entries, while the trade already on keeps its stop, its target and its
    exit rules and closes on the rules that opened it.
    """
    _use_json(as_json)
    settings = get_settings()
    try:
        service = CommandQueueService(connect(settings.db_path), YFinanceProvider())
        message = service.remove(symbol)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json({"symbol": symbol.upper(), "message": message})
    else:
        console.print(message)


@engine_app.command("apply-queue")
def engine_apply_queue(
    notify: Annotated[
        bool, typer.Option("--notify", help="Confirm each applied command in the chat.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Apply the commands the Telegram bot queued for this engine.

    The bot cannot open the database — it is resident while a scan is not, and
    DuckDB has a single writer — so it leaves intents in TRD_HOME/commands and
    this drains them. Run it before the scan, so a name added from chat is in the
    universe the very pass that follows.
    """
    _use_json(as_json)
    settings = get_settings()
    try:
        service = CommandQueueService(connect(settings.db_path), YFinanceProvider())
        results = service.drain(settings.home)
    except TrdError as exc:
        _fail(exc)
        return

    if as_json:
        _emit_json(
            [
                {
                    "update_id": r.command.update_id,
                    "kind": r.command.kind,
                    "symbol": r.command.symbol,
                    "ok": r.ok,
                    "message": r.message,
                }
                for r in results
            ]
        )
        return

    if not results:
        console.print("No queued commands.")
        return
    label = None
    with suppress(TrdError):  # an engine can be unconfigured and still have a queue
        label = label_from_env(service.engine.config().exit_params)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() if notify else ""
    for result in results:
        mark = "[green]✓[/green]" if result.ok else "[red]✗[/red]"
        console.print(f"{mark} {result.message}")
        if not token:
            continue
        # Back to whoever typed it, not to TELEGRAM_CHAT_ID: that is the fills
        # channel, and a one-way broadcast is the wrong place to answer a
        # question one person asked. In a private chat the chat id *is* the
        # user id, so the queued command already carries the address.
        try:
            TelegramNotifier(token, str(result.command.user_id)).send(
                f"[{label}] {'✅' if result.ok else '⚠️'} {result.message}"
            )
        except TrdError as exc:
            # Same rule as the scan: the command is applied and recorded. An
            # undelivered confirmation must not fail the run that follows it.
            err_console.print(f"[yellow]warning:[/yellow] confirmation failed: {exc}")


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
    exits_only: Annotated[
        bool,
        typer.Option(
            "--exits-only",
            help="Manage exits and rank, but take no new entries — for a pass forced "
            "outside the session, where a fill would sit on a stale quote until Monday.",
        ),
    ] = False,
) -> None:
    """Run one scan pass: manage exits, then take the best new entries."""
    try:
        quotes = _prefetched_quotes(lambda s: s.quote_symbols())
        service = _engine_service()
        result = service.scan(paper=paper, quotes=quotes, entries=not exits_only)
    except TrdError as exc:
        _fail(exc)
        return
    _emit_scan(result, as_json, ndjson)
    if notify:
        config = service.config()
        _notify_scan(result, label_from_env(config.exit_params, live=config.is_live))


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
    live: Annotated[bool, typer.Option("--live/--no-live", help="Live view on a terminal.")] = True,
) -> None:
    """Scan on a loop. Ctrl-C to stop — every pass is already persisted.

    On a terminal this is a live view: the book stays still and the clock,
    capacity and activity feed move, so a fill is something you notice rather
    than something you scroll back to find. Piped, or with --ndjson, it falls
    back to plain scrolling output — a live display in a pipe is garbage.
    """
    import time

    live_view = live and console.is_terminal and not ndjson
    count = 0
    activity: list[str] = []
    if not ndjson and not live_view:
        console.print(f"[dim]Monitoring every {interval}s. Ctrl-C to stop.[/dim]")

    def one_pass() -> tuple[ScanResult, list, int, dict, str]:
        # Connect per pass, not once for the whole loop: a monitor that held the
        # connection would keep DuckDB's single writer lock for its entire run,
        # blocking every other trd command for as long as it watches.
        quotes = _prefetched_quotes(lambda s: s.quote_symbols())
        service = _engine_service()
        try:
            result = service.scan(paper=paper, quotes=quotes)
            rows = service.position_rows(quotes=quotes)
            config = service.config()
            return result, rows, config.max_positions, config.exit_params, service.account().name
        finally:
            service.conn.close()

    def note(result: ScanResult) -> None:
        """Newest first, and only things that actually happened."""
        stamp = result.at.strftime("%H:%M:%S")
        for fill in result.closed:
            r = f" ({fill.r_multiple:+.2f}R)" if fill.r_multiple is not None else ""
            activity.insert(0, f"{stamp}|[red]SELL[/red] {fill.symbol} {fill.rule or ''}{r}")
        for fill in result.opened:
            activity.insert(0, f"{stamp}|[green]BUY[/green] {fill.symbol} {fill.strategy}")
        for signal in result.signals:
            if not signal.acted:
                activity.insert(
                    0, f"{stamp}|[dim]signal {signal.symbol} {signal.strategy} — not taken[/dim]"
                )

    live_ctx = (
        Live(console=console, refresh_per_second=4, transient=False) if live_view else nullcontext()
    )
    try:
        with live_ctx as display:
            while passes is None or count < passes:
                try:
                    result, rows, max_positions, params, account = one_pass()
                except TrdError as exc:
                    _fail(exc)
                    return
                count += 1
                note(result)
                if not live_view:
                    _emit_scan(result, as_json=False, ndjson=ndjson)
                if notify:
                    live = result.mode == EngineMode.LIVE
                    _notify_scan(result, label_from_env(params, live=live))
                if passes is not None and count >= passes:
                    if live_view and display is not None:
                        display.update(
                            engine_monitor_view(
                                rows,
                                max_positions,
                                count,
                                result.at,
                                0,
                                activity,
                                build_version(),
                                account,
                                int(params.get("flat_at_minute", 0)) > 0,
                                console.size.width,
                            )
                        )
                    break
                if not live_view:
                    time.sleep(interval)
                    continue
                # Tick the clock down rather than freezing between scans: a view
                # that has not moved for a minute is indistinguishable from one
                # that has died.
                for remaining in range(interval, 0, -1):
                    assert display is not None
                    display.update(
                        engine_monitor_view(
                            rows,
                            max_positions,
                            count,
                            result.at,
                            remaining,
                            activity,
                            build_version(),
                            account,
                            int(params.get("flat_at_minute", 0)) > 0,
                            console.size.width,
                        )
                    )
                    time.sleep(1)
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


@engine_app.command("intents")
def engine_intents(
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            "-s",
            help="Only this status: pending, submitted, awaiting_approval, filled, "
            "rejected, cancelled, expired.",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Order intents a live engine has written, newest first.

    A live engine fills nothing itself: each entry or exit it decides on is
    recorded here with the price the scan saw, and stays open until the broker's
    answer resolves it. An open buy holds a slot and reserves its cash.
    """
    _use_json(as_json)
    service = _engine_service()
    try:
        rows = service.intent_rows(status=status)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(rows)
        return
    if not rows:
        console.print(
            "No order intents"
            + (f" with status {status}" if status else "")
            + ". A live engine writes one per entry or exit; a paper engine never does."
        )
        return
    console.print(engine_intents_table(rows))


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
    console.print(
        engine_positions_table(
            rows,
            title,
            show_exit=all_,
            max_positions=max_positions,
            terminal_width=console.size.width,
        )
    )


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


@engine_app.command("edge")
def engine_edge(
    samples: Annotated[
        int, typer.Option("--samples", "-k", help="Random entries drawn per taken signal.")
    ] = EDGE_SAMPLES,
    seed: Annotated[
        int, typer.Option("--seed", help="Random seed — same seed, same draws, same answer.")
    ] = EDGE_SEED,
    as_json: JsonOpt = False,
) -> None:
    """Do the entries beat a random entry in the same name on the same session?

    Every signal the engine acted on is set beside K random entries in the same
    symbol and session, restricted to bars the engine could have entered on, each
    priced with the stop plan_entry would have set and walked by the same function
    as the real signal. Paired per signal, so the edge is what the rule's timing
    added. Read-only. 'trd learn entry-edge'.
    """
    _use_json(as_json)
    db_path = get_settings().db_path
    try:
        if not db_path.exists():
            raise TrdError(f"No database at {db_path}. Run 'trd init' first.")
        conn = connect_read_only(db_path)
        with _spinner("Drawing random entries..."):
            report = EdgeService(conn).report(samples=samples, seed=seed)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(report)
        return
    console.print(edge_table(report))
    if report.unmeasured:
        console.print(
            f"[dim]{report.unmeasured} taken signal(s) had no stop to price or no bar to "
            "draw from and are left out of both sides.[/dim]"
        )


@engine_app.command("daily-report")
def engine_daily_report(
    date_str: Annotated[
        str | None,
        typer.Option("--date", "-d", help="Session to report (YYYY-MM-DD). Default: today."),
    ] = None,
    engines: Annotated[
        str | None,
        typer.Option(
            "--engines",
            help="Engines to cover: 'swing=/path,day=/path'. Default: TRD_BOT_ENGINES, "
            "else this TRD_HOME alone.",
        ),
    ] = None,
    window: Annotated[
        int,
        typer.Option("--window", help="Days of closed trades behind 'what is working'."),
    ] = DEFAULT_WINDOW_DAYS,
    notify: Annotated[
        bool, typer.Option("--notify", help="Send the report to the configured chat (Telegram).")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """The post-market report: up or down, what worked, what did not, what is at risk.

    One message a day covering every engine, because a fill alert says a trade
    happened and never whether the day was good. Facts only — no judgement about
    whether a decision was right, which needs measurements the engine does not
    take yet.

    Nothing is sent on a date no engine has a bar for: a report that posts
    "flat, nothing happened" on Thanksgiving trains its reader to ignore it.
    """
    _use_json(as_json)
    on = (_parse_date(date_str) or datetime.now()).date()
    try:
        targets = _report_targets(engines)
    except TrdError as exc:
        _fail(exc)
        return

    days: list[EngineDay] = []
    for name, home in targets:
        db_path = home / "trd.duckdb"
        if not db_path.exists():
            # Never `connect` a path that is not there: it would create an empty
            # database in a directory that was probably a typo, and then report
            # a healthy engine with no trades.
            days.append(failed_engine(name, f"no database at {db_path}"))
            continue
        service = None
        try:
            service = EngineService(connect(db_path), YFinanceProvider())
            days.append(engine_day(service, name, on, window_days=window))
        except TrdError as exc:
            days.append(failed_engine(name, str(exc)))
        finally:
            if service is not None:
                service.conn.close()

    report = DailyReport(
        on=on,
        window_days=window,
        engines=days,
        live_twin=os.environ.get("TRD_LIVE_TWIN", "").strip() or DEFAULT_LIVE_TWIN,
    )
    if as_json:
        _emit_json(report)
        return

    message = daily_report_message(report)
    # Neither markup nor highlighting: an engine named in square brackets is
    # Rich markup, and the terminal would silently eat '[swing]'.
    console.print(message, markup=False, highlight=False)
    if not notify:
        return
    if not report.market_open:
        console.print("[dim]No session stored for this date — nothing sent.[/dim]")
        return
    notifier = notify_from_env()
    if notifier is None:
        err_console.print(
            "[yellow]warning:[/yellow] --notify set but TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID are not configured — nothing sent."
        )
        return
    try:
        notifier.send(message)
    except TrdError as exc:
        err_console.print(f"[yellow]warning:[/yellow] notification failed: {exc}")


def _report_targets(spec: str | None) -> list[tuple[str, Path]]:
    """Which engines the daily report covers, and where their homes are.

    `--engines` first, then TRD_BOT_ENGINES — the same variable the bot
    Deployment already sets, so the report job mounts and names the engines
    exactly as chat does and the two cannot drift. With neither, it reports the
    single engine this TRD_HOME points at, which is what makes the command
    runnable by hand.
    """
    raw = (spec or "").strip()
    env = dict(os.environ) if not raw else {"TRD_BOT_ENGINES": raw}
    targets = engines_from_env(env)
    if targets:
        return [(t.name, t.home) for t in targets]
    # TRD_ENGINE_LABEL, or a neutral name — deliberately not label_from_env(),
    # whose day/swing fallback reads the rule set, which is not open yet here.
    return [(os.environ.get("TRD_ENGINE_LABEL", "").strip() or "engine", get_settings().home)]


@engine_app.command("live-report")
def engine_live_report(
    date_str: Annotated[
        str | None,
        typer.Option("--date", "-d", help="Session to report (YYYY-MM-DD). Default: today."),
    ] = None,
    notify: Annotated[
        bool, typer.Option("--notify", help="Send it to the private live alert chat.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """The LIVE engine's post-market message: the account, then every position.

    Capital, worth and return as the daily report defines them, today's move,
    cash and orders in flight — then each open position with its day change,
    P&L, R, stop in force (↑ = trailing), distance to stop and target, and money
    at risk — then the session's buys, exits and orders that never filled.

    READ-ONLY connection, no network. Refused on a paper engine. --notify goes
    to the private alert chat (TRD_LIVE_ALERT_CHAT_ID, else the first
    TRD_BOT_ALLOWED_USER_IDS), never the channel, and sends nothing on a date
    the engine has no bar for.
    """
    _use_json(as_json)
    on = (_parse_date(date_str) or datetime.now()).date()
    label = os.environ.get("TRD_ENGINE_LABEL", "").strip() or LIVE_LABEL
    db_path = get_settings().db_path
    conn = None
    try:
        if not db_path.exists():
            raise TrdError(f"No database at {db_path}. Run 'trd init' first.")
        conn = connect_read_only(db_path)
        report = live_report(EngineService(conn, YFinanceProvider()), label, on)
    except TrdError as exc:
        _fail(exc)
        return
    finally:
        if conn is not None:
            conn.close()

    message = live_report_message(report, label)
    sent = False
    if notify and report.market_open:
        refused: list[str] = []
        notifier = alert_notifier_from_env(error=refused.append)
        if notifier is None:
            _fail(TrdError(refused[0] if refused else "No TELEGRAM_BOT_TOKEN set."))
            return
        try:
            notifier.send(message, html=True)
        except TrdError as exc:  # NotifyError: delivery failed, token never shown
            _fail(exc)
            return
        sent = True
    if as_json:
        _emit_json({"message": message, "sent": sent, "report": report.model_dump(mode="json")})
        return
    console.print(strip_html(message), markup=False, highlight=False)
    if notify and not report.market_open:
        console.print("[dim]No session stored for this date — nothing sent.[/dim]")


@engine_app.command("outcomes")
def engine_outcomes(
    backfill: Annotated[
        bool,
        typer.Option("--backfill", help="Measure every trade and signal not measured yet."),
    ] = False,
    recompute: Annotated[
        bool,
        typer.Option("--recompute", help="Re-measure everything, even rows already stored."),
    ] = False,
    trades: Annotated[
        bool, typer.Option("--trades", help="Per-trade detail instead of the averages.")
    ] = False,
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="How many trades to show with --trades.")
    ] = 25,
    as_json: JsonOpt = False,
) -> None:
    """Did we enter too early, did we exit too early, and what did the signals we
    passed over go on to do.

    MAE is the heat a trade took before it worked, MFE is the best it ever
    looked, capture is the share of that it kept, and follow-through is where
    price went after the exit. All in R, so a $10 day trade and a $100 swing are
    the same measurement. 'trd learn mae' defines each one.

    The signals table is the uncomfortable one: every signal the engine declined
    is stored, and this walks them forward against the stop the engine itself
    would have set. Signals that fired with the book already full are counted
    separately — they could not have been taken, whatever they did next.

    Nothing here judges a trade. It is arithmetic, so that anything reasoning
    over it is arguing from measurement rather than from memory.
    """
    _use_json(as_json)
    service = OutcomeService(connect(get_settings().db_path))
    try:
        if backfill or recompute:
            with _spinner("Measuring trades and signals..."):
                stats = service.backfill(recompute=recompute)
            if not as_json:
                console.print(
                    f"Measured [bold]{stats.trades_measured}[/bold] trade(s) and "
                    f"[bold]{stats.signals_measured}[/bold] signal(s); "
                    f"{stats.trades_skipped + stats.signals_skipped} already done or "
                    "not measurable."
                )
        if trades:
            rows = service.trade_rows(limit=limit)
            if as_json:
                _emit_json([o for o, _, _ in rows])
                return
            if not rows:
                console.print(
                    "Nothing measured yet. Run [bold]trd engine outcomes --backfill[/bold]."
                )
                return
            console.print(outcome_trades_table(rows))
            return
        summary = service.summary()
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(summary)
        return
    if summary.measured_trades == 0 and summary.measured_signals == 0:
        console.print("Nothing measured yet. Run [bold]trd engine outcomes --backfill[/bold].")
        return
    for renderable in outcome_summary_renderables(summary):
        console.print(renderable)


ReviewDateOpt = Annotated[
    str | None,
    typer.Option("--date", "-d", help="Session to review (YYYY-MM-DD). Default: today."),
]
ReviewEnginesOpt = Annotated[
    str | None,
    typer.Option(
        "--engines",
        help="Engines to cover: 'swing=/path,day=/path'. Default: TRD_BOT_ENGINES, "
        "else this TRD_HOME alone.",
    ),
]
ReviewWindowOpt = Annotated[
    int, typer.Option("--window", help="Days of closed trades a finding may be drawn from.")
]


def _packs(engines: str | None, on: date, window: int) -> tuple[list[EnginePack], list[str]]:
    """Build one pack per engine, each on its own short-lived connection.

    Read-only, and enforced by the connection rather than promised: this reaches
    into both engines' databases, and a review is never a reason to write to one.
    The snapshot is a separate, explicit step that opens its own writable
    connection only when `--snapshot` asks for it.
    """
    packs: list[EnginePack] = []
    problems: list[str] = []
    for name, home in _report_targets(engines):
        db_path = home / "trd.duckdb"
        if not db_path.exists():
            problems.append(f"{name}: no database at {db_path}")
            continue
        conn = None
        try:
            conn = connect_read_only(db_path)
            packs.append(
                engine_pack(
                    EngineService(conn, YFinanceProvider()),
                    OutcomeService(conn),
                    name,
                    on,
                    window_days=window,
                )
            )
        except TrdError as exc:
            problems.append(f"{name}: {exc}")
        except duckdb.Error as exc:
            # Usually a database written by an older build, which a read-only
            # connection cannot bring forward. Name the command that can.
            problems.append(
                f"{name}: {str(exc).splitlines()[0]} — run 'trd engine outcomes --backfill' "
                "against this home first, which migrates it."
            )
        finally:
            if conn is not None:
                conn.close()
    return packs, problems


@engine_app.command("watchdog")
def engine_watchdog(
    engines: ReviewEnginesOpt = None,
    max_age: Annotated[
        int,
        typer.Option("--max-age", help="Minutes of silence that counts as stopped. Default 15."),
    ] = WATCHDOG_MAX_AGE,
    repeat: Annotated[
        int,
        typer.Option("--repeat", help="Minutes before an unchanged alert is sent again."),
    ] = WATCHDOG_REPEAT,
    notify: Annotated[
        bool, typer.Option("--notify", help="Alert the configured chat (Telegram).")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Has a scan landed recently? Run this from OUTSIDE the cluster.

    The engine went dark twice in three days and nothing said so, because
    everything that could have noticed lived inside the thing that had stopped.
    This reads each engine's published `status.json` — never a database, so it
    can never block a scan — and complains when the market is open and no scan
    has landed.

    Exits non-zero when an engine is failing, so a launchd agent or a cron entry
    can act on it without parsing anything. Silence outside market hours is
    correct and is never reported.
    """
    _use_json(as_json)
    try:
        targets = _report_targets(engines)
    except TrdError as exc:
        _fail(exc)
        return

    result = watchdog_check(targets, max_age=max_age)
    state_path = targets[0][1] / WATCHDOG_STATE_FILENAME
    state = WatchdogState.load(state_path)
    message, next_state = watchdog_decide(result, state, repeat_minutes=repeat)

    if as_json:
        _emit_json(result)
    else:
        for pulse in result.engines:
            mark = "[green]✓[/green]" if pulse.ok else "[red]✗[/red]"
            detail = pulse.problem or f"last scan {pulse.age_minutes} minutes ago"
            console.print(f"{mark} {pulse.engine}: {detail}", highlight=False)
            for alarm in pulse.alarms:
                console.print(f"  [yellow]⚠[/yellow] {alarm}", highlight=False, markup=True)
        if not result.in_session:
            console.print("[dim]Outside market hours — no scan is expected.[/dim]")

    if notify and message is not None:
        notifier = notify_from_env()
        if notifier is None:
            err_console.print(
                "[yellow]warning:[/yellow] --notify set but TELEGRAM_BOT_TOKEN / "
                "TELEGRAM_CHAT_ID are not configured — nothing sent."
            )
        else:
            try:
                notifier.send(message)
                next_state.save(state_path)
            except TrdError as exc:
                err_console.print(f"[yellow]warning:[/yellow] alert failed: {exc}")
    elif notify:
        next_state.save(state_path)

    if not result.healthy and result.in_session:
        raise typer.Exit(1)


@engine_app.command("review-pack")
def engine_review_pack(
    date_str: ReviewDateOpt = None,
    engines: ReviewEnginesOpt = None,
    window: ReviewWindowOpt = DEFAULT_REVIEW_WINDOW,
    as_json: JsonOpt = False,
) -> None:
    """Everything a reviewer needs about one session, as one document.

    Every trade that closed with its outcome metrics, every signal that fired —
    taken and passed over — with the reason recorded at the time, the rule that
    fired quoted with its own stated intent, the config in force, and the day's
    P&L from the same code the Telegram report reads.

    A document rather than a set of queries, because an agent is only
    reproducible if its input is: this is what `trd engine review` and anything
    reasoning over it consume, and it can be saved, diffed and replayed.
    """
    _use_json(as_json)
    on = (_parse_date(date_str) or datetime.now()).date()
    packs, problems = _packs(engines, on, window)
    pack = ReviewPack(on=on, generated_at=datetime.now(), build=build_version(), engines=packs)
    if as_json:
        _emit_json(pack)
        return
    for problem in problems:
        err_console.print(f"[yellow]warning:[/yellow] {problem}")
    if not packs:
        console.print("No engine could be read.")
        return
    for engine in packs:
        console.print(
            f"[bold]{engine.engine}[/bold] {engine.on} — {len(engine.trades)} closed, "
            f"{len(engine.signals)} signals, {len(engine.window_trades)} measured trades "
            f"in the {engine.window_days}d window"
        )
        for caveat in engine.caveats:
            console.print(f"[dim]· {caveat}[/dim]")
    console.print("[dim]--json emits the whole document.[/dim]")


@engine_app.command("agenda")
def engine_agenda(
    engines: ReviewEnginesOpt = None,
    window: Annotated[
        int,
        typer.Option("--window", help="How many stored review sessions to consider."),
    ] = DEFAULT_WINDOW_SESSIONS,
    min_sessions: Annotated[
        int,
        typer.Option("--min-sessions", help="Sessions a finding must survive to be filed."),
    ] = DEFAULT_MIN_SESSIONS,
    as_json: JsonOpt = False,
) -> None:
    """Which review findings have earned a ticket, and which have gone away.

    The review recomputes from scratch nightly and does not remember yesterday,
    so a problem true for a month is reported as a fresh discovery every night.
    This reads the stored reviews back, groups every finding by its key, and
    asks whether it survived: READY is work, WATCHING is not yet proven, and
    QUIET is a finding that stopped firing — the only evidence in trd that a
    change to the rules actually did something.

    Read-only, no network, and it knows nothing about any issue tracker: it
    emits the agenda and something else files it.
    """
    _use_json(as_json)
    try:
        targets = _report_targets(engines)
    except TrdError as exc:
        _fail(exc)
        return

    snapshots: list[tuple[str, list[tuple[date, dict]]]] = []
    problems: list[str] = []
    for name, home in targets:
        db_path = home / "trd.duckdb"
        if not db_path.exists():
            problems.append(f"{name}: no database at {db_path}")
            continue
        conn = None
        try:
            conn = connect_read_only(db_path)
            snapshots.append((name, ReviewSnapshotRepo(conn).recent_payloads(limit=window)))
        except (TrdError, duckdb.Error) as exc:
            problems.append(f"{name}: {exc}")
        finally:
            if conn is not None:
                conn.close()

    built = agenda(
        snapshots,
        on=datetime.now().date(),
        window=window,
        min_sessions=min_sessions,
        caveats=problems,
    )
    if as_json:
        _emit_json(built)
        return
    for problem in problems:
        err_console.print(f"[yellow]warning:[/yellow] {problem}")
    for renderable in agenda_renderables(built):
        console.print(renderable, markup=True, highlight=False)


@engine_app.command("review")
def engine_review(
    date_str: ReviewDateOpt = None,
    engines: ReviewEnginesOpt = None,
    window: ReviewWindowOpt = DEFAULT_REVIEW_WINDOW,
    snapshot: Annotated[
        bool,
        typer.Option("--snapshot", help="Store the review, dated, in each engine's database."),
    ] = False,
    ai: Annotated[
        bool,
        typer.Option("--ai", help="Also ask a model what the numbers mean (needs the 'ai' extra)."),
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help="Model to reason with: any pydantic-ai 'provider:model', 'claude-code:opus' "
            "(the local binary, on the subscription) or 'vertex:<model>'. "
            "Default: TRD_AI_MODEL, else Opus 5.",
        ),
    ] = None,
    notify: Annotated[
        bool,
        typer.Option(
            "--notify",
            help="Send the review to the configured chat (Telegram). Nothing is sent on a "
            "date no engine has a bar for.",
        ),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """What the day's decisions say about the rules — statistics, no judgement.

    Findings are about a rule, a strategy or the entry filter, never about one
    trade: "SOFI stopped out, consider a wider stop" written forty times a month
    buries the one real finding. Each carries the population it was drawn from
    and the backtest that would settle it.

    A pattern drawn from fewer than 20 trades is labelled a HYPOTHESIS. Below 5
    it is not reported at all. And "nothing conclusive" is a real answer — it is
    the expected one on most days, because two engines and a handful of trades
    are mostly noise.
    """
    _use_json(as_json)
    on = (_parse_date(date_str) or datetime.now()).date()
    packs, problems = _packs(engines, on, window)
    result = review(packs, on)
    result.caveats.extend(problems)

    # The model runs on top of the arithmetic, never instead of it: the
    # deterministic findings are computed first and handed over as input, so a
    # provider outage costs the judgement and not the review.
    judged = _ai_review(packs, result, on, model) if ai and packs else None

    if snapshot:
        _store_review(engines, on, result, packs, judged)

    if as_json:
        _emit_json(
            {"review": result.model_dump(mode="json"), "ai": judged.model_dump(mode="json")}
            if judged
            else result
        )
        return
    for problem in problems:
        err_console.print(f"[yellow]warning:[/yellow] {problem}")
    if not packs:
        console.print("No engine could be read.")
        return
    for renderable in review_renderables(result, packs):
        console.print(renderable, markup=True, highlight=False)
    if judged is not None:
        for renderable in ai_review_renderables(judged):
            console.print(renderable, markup=True, highlight=False)
    if notify:
        _notify_review(result, packs, judged, on)


def _notify_review(result: ReviewResult, packs: list[EnginePack], judged: Any, on: date) -> None:
    """One message, the arithmetic then the model's read — or nothing at all.

    The same rule as the daily report: a date no engine has a bar for is a
    holiday or a sync that has not landed, and a review that posted "nothing
    conclusive" every Thanksgiving would train its reader to ignore it.
    """
    if not any(pack.day.last_session == on for pack in packs):
        console.print("[dim]No session stored for this date — nothing sent.[/dim]")
        return
    notifier = notify_from_env()
    if notifier is None:
        err_console.print(
            "[yellow]warning:[/yellow] --notify set but TELEGRAM_BOT_TOKEN / "
            "TELEGRAM_CHAT_ID are not configured — nothing sent."
        )
        return
    try:
        notifier.send(review_message(result, packs, judged))
    except TrdError as exc:
        err_console.print(f"[yellow]warning:[/yellow] notification failed: {exc}")


def _ai_review(packs: list[EnginePack], result: ReviewResult, on: date, model: str | None):
    """Ask a model what the day meant, or explain why it could not be asked.

    Imported here rather than at module scope: `trd.agents.review_agent` imports
    pydantic-ai, and every other command in this CLI must keep working on a
    machine that has never installed it.
    """
    try:
        from trd.agents.review_agent import run_ai_review
    except ImportError:
        err_console.print(
            "[yellow]warning:[/yellow] --ai needs the optional extra: "
            "[bold]uv sync --extra ai[/bold] (or pip install 'trd[ai]'). "
            "The deterministic review above is unaffected."
        )
        return None
    pack = ReviewPack(on=on, generated_at=result.generated_at, build=result.build, engines=packs)
    try:
        with _spinner("Reading the day..."):
            return run_ai_review(pack, result, model=model)
    except TrdError as exc:
        err_console.print(f"[yellow]warning:[/yellow] {exc}")
    except Exception as exc:
        # Never fatal. The arithmetic is the review; the model is commentary on
        # it, and a bad night at the provider must not cost the day's numbers.
        err_console.print(f"[yellow]warning:[/yellow] the model could not be reached: {exc}")
    return None


def _store_review(
    engines: str | None,
    on: date,
    result: ReviewResult,
    packs: list[EnginePack],
    judged: Any = None,
) -> None:
    """Write each engine its own slice of the review, dated.

    Per engine, because one database is one engine: half a review stored in each
    would make either one alone a lie. The point of storing it at all is that a
    claim made on Tuesday can be scored on Friday against what happened — which
    is also the only way to find out whether the reviewer is any good.
    """
    by_engine = {p.engine: p for p in packs}
    for name, home in _report_targets(engines):
        pack = by_engine.get(name)
        if pack is None:
            continue
        findings = [f for f in result.findings if f.engine == name]
        # The engine's own slice, not the combined review. Storing every
        # engine's findings in every database made each one claim the other's
        # work: `Finding.key` exists so a claim can be tracked across days, and
        # a history that double-counts and misattributes cannot do that. The
        # denormalized columns below were always filtered; the payload was not.
        mine = result.model_copy(update={"findings": findings, "engines": [name]})
        conn = None
        try:
            conn = connect(home / "trd.duckdb")
            ReviewSnapshotRepo(conn).save(
                ReviewSnapshotRow(
                    snapshot_date=on,
                    generated_at=result.generated_at,
                    build=result.build,
                    engine=name,
                    trades_closed=len(pack.trades),
                    signals_fired=len(pack.signals),
                    findings=sum(1 for f in findings if not f.hypothesis),
                    hypotheses=sum(1 for f in findings if f.hypothesis),
                    quiet=not findings,
                ),
                payload={
                    "review": mine.model_dump(mode="json"),
                    "pack": pack.model_dump(mode="json"),
                    # Stored beside the arithmetic it was given, so a claim the
                    # model made can be read back next to the numbers it saw.
                    "ai": judged.model_dump(mode="json") if judged else None,
                },
            )
        except TrdError as exc:
            err_console.print(f"[yellow]warning:[/yellow] could not store {name}'s review: {exc}")
        finally:
            if conn is not None:
                conn.close()


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


@engine_app.command("cash")
def engine_cash(
    set_to: Annotated[
        str | None,
        typer.Option("--set", help="Turn the ledger on (or re-base it) at this capital."),
    ] = None,
    off: Annotated[bool, typer.Option("--off", help="Turn the settled-cash ledger off.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Settled cash: what this engine may spend now, and what is waiting on T+1.

    In a cash account a sale's proceeds settle one business day later and cannot
    fund a buy until they do. With a capital set, scans and backtests skip any
    entry the settled cash cannot pay for. --set and --off change the capital
    only; every other part of the config is carried over untouched.
    """
    _use_json(as_json)
    if set_to is not None and off:
        _fail(TrdError("Give --set or --off, not both."))
        return
    service = _engine_service()
    try:
        if set_to is not None:
            service.set_capital(_parse_decimal(set_to, "capital"))
        elif off:
            service.set_capital(None)
        cash = service.cash()
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        # Off is `capital: null`, the same NULL the schema stores for it, so one
        # check reads both shapes.
        _emit_json(cash if cash is not None else {"capital": None})
        return
    for renderable in engine_cash_renderables(cash):
        console.print(renderable)


@engine_app.command("halt")
def engine_halt(
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why, in your words. Shown wherever it is.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Throw a live engine's kill switch.

    While halted the scan writes no buy intents (exits are still decided and
    recorded, so the book shows what the engine wants) and 'trd engine outbox'
    emits nothing at all. Survives restarts, reconfiguring and 'trd backup'.
    'trd engine resume' releases it. Paper engines are refused.
    """
    _use_json(as_json)
    service = _engine_service()
    try:
        guard = service.halt(reason)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(guard)
        return
    console.print(f"[bold red]HALTED[/bold red] — {guard.halt_reason}", highlight=False)


@engine_app.command("resume")
def engine_resume(as_json: JsonOpt = False) -> None:
    """Release a live engine's kill switch. The caps stay as they were.

    After a loss-stop halt, the next scan halts it again unless the loss is back
    inside --max-loss or the cap has been raised ('trd engine guard --max-loss').
    """
    _use_json(as_json)
    service = _engine_service()
    try:
        guard = service.resume()
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(guard)
        return
    console.print("Resumed — the kill switch is off.")


@engine_app.command("guard")
def engine_guard(
    max_order: Annotated[
        str | None, typer.Option("--max-order", help="Most one buy order may cost, in dollars.")
    ] = None,
    max_daily: Annotated[
        str | None,
        typer.Option("--max-daily", help="Most the engine may buy in one session, in dollars."),
    ] = None,
    max_positions: Annotated[
        int | None,
        typer.Option("--max-positions", help="Most open positions + open buy orders."),
    ] = None,
    max_loss: Annotated[
        str | None,
        typer.Option(
            "--max-loss",
            help="Dollars of realized + unrealized loss at which the engine halts itself.",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """A live engine's hard caps: show them, or set any of them.

    Enforced by trd whatever the strategy config says, twice: the scan will not
    write a buy that breaks one, and the outbox withholds a pending buy that
    breaks one now. Caps apply to buys only — an exit is never withheld. No flags
    shows the caps in force. Paper engines are refused.
    """
    _use_json(as_json)
    service = _engine_service()
    try:
        guard = service.set_guard(
            max_order=_parse_decimal(max_order, "max-order") if max_order else None,
            max_daily=_parse_decimal(max_daily, "max-daily") if max_daily else None,
            max_positions=max_positions,
            max_loss=_parse_decimal(max_loss, "max-loss") if max_loss else None,
        )
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(guard)
        return
    for renderable in engine_guard_renderables(guard):
        console.print(renderable)


@engine_app.command("reconcile")
def engine_reconcile(
    snapshot: Annotated[
        Path,
        typer.Argument(
            help="Broker snapshot JSON: {as_of, source, cash, positions:[{symbol, quantity}]}."
        ),
    ],
    account: Annotated[
        str | None,
        typer.Option("--account", help="Which trd account to compare. Defaults to the engine's."),
    ] = None,
    compare_cash: Annotated[
        bool,
        typer.Option(
            "--compare-cash",
            help="Also compare the snapshot's cash/unsettled with the engine's settled-cash "
            "ledger. Only for a broker account whose cash is the engine's alone.",
        ),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Diff a brokerage against what trd believes it holds.

    The gap no backtest shows: a paper book can be perfectly consistent with
    itself and still describe a portfolio that does not exist. Reading the
    broker is an authenticated, interactive step done outside trd (see
    docs/robinhood-mcp.md); this command does the deterministic comparison, so
    it needs no network and no credentials.

    --compare-cash also diffs the broker's settled cash (and unsettled, when the
    snapshot has it) against the engine's T+1 ledger, beyond $0.05 a CASH
    MISMATCH. Off by default: the account funding an engine usually holds more
    than the engine's capital ($300 deposited for a $100 engine), and that
    surplus is not a mismatch. Use it when the account's cash is dedicated.

    Exits non-zero when the two disagree, so a scheduled check can act on it.
    """
    _use_json(as_json)
    try:
        text = snapshot.read_text()
    except OSError as exc:
        _fail(TrdError(f"{snapshot}: {exc.strerror or exc}"))
        return
    try:
        parsed = BrokerSnapshot.model_validate_json(text)
    except ValidationError as exc:
        _fail(TrdError(f"{snapshot}: not a broker snapshot ({exc.error_count()} problems) — {exc}"))
        return

    service = _reconcile_service()
    try:
        engine = EngineService(service.conn, YFinanceProvider())
        name = account or engine.account().name
        ledger = None
        if compare_cash:
            ledger = engine.cash()
            if ledger is None:
                raise TrdError(
                    "--compare-cash needs the engine's settled-cash ledger, and this engine "
                    "has no capital ('trd engine cash --set')."
                )
        result = service.reconcile(parsed, name, cash=ledger)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(result)
    else:
        for renderable in reconcile_renderables(result):
            console.print(renderable)
    if not result.in_sync:
        raise typer.Exit(code=1)


@engine_app.command("fills")
def engine_fills(
    snapshot: Annotated[
        Path,
        typer.Argument(
            help="Broker snapshot JSON, as the broker's reads return it: "
            "{orders:[...], approvals:[...], taken_at}."
        ),
    ],
    notify: Annotated[
        bool,
        typer.Option(
            "--notify",
            help="Post each fill to the channel, and anything rejected, expired or "
            "conflicting to the live alert chat (Telegram).",
        ),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Ingest a live engine's broker fills: resolve its order intents, write the
    txns, and open or close positions at the price the broker actually gave.

    The snapshot is the brokerage's own order and trade-approval reads written
    to a file unchanged (see docs/robinhood-mcp.md). Orders match an intent by
    its ref id, its approval id, or a broker order id already recorded. Orders
    and approvals that match no intent are reported and left alone. Re-ingesting
    the same snapshot changes nothing.

    Exits non-zero when anything conflicts (symbol or side disagree, a sell for
    a position already closed): those are shown and never applied.
    """
    _use_json(as_json)
    try:
        text = snapshot.read_text()
    except OSError as exc:
        _fail(TrdError(f"{snapshot}: {exc.strerror or exc}"))
        return
    try:
        parsed = FillSnapshot.model_validate_json(text)
    except ValidationError as exc:
        _fail(TrdError(f"{snapshot}: not a fill snapshot ({exc.error_count()} problems) — {exc}"))
        return

    settings = get_settings()
    service = FillIngestService(connect(settings.db_path))
    try:
        result = service.ingest(parsed, datetime.now())
    except TrdError as exc:
        _fail(exc)
        return
    finally:
        service.conn.close()
    if as_json:
        _emit_json(result)
    else:
        for renderable in fill_ingest_renderables(result):
            console.print(renderable)
    if notify:
        label = label_from_env(live=True)
        _send(fill_messages(result, label))
        alert = fill_alert_message(result, label)
        _send([alert] if alert else [], alert=True)
        if result.auto_halt:
            _send([halt_message(result.auto_halt, label)], alert=True)
    if not result.ok:
        raise typer.Exit(code=1)


outbox_app = typer.Typer(
    help="A live engine's pending orders, for an agent session to place.",
)
engine_app.add_typer(outbox_app, name="outbox")


@outbox_app.callback(invoke_without_command=True)
def engine_outbox(
    ctx: typer.Context,
    include_stale: Annotated[
        bool,
        typer.Option(
            "--include-stale",
            help="List buys older than the stale limit too (flagged in their note).",
        ),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """The live engine's PENDING intents as order specs an agent passes straight
    to the broker's order tool: ref_id, symbol, side, quantity, type, time in
    force, market hours — plus what a person approving it wants to see.

    Oldest first. An intent past its 16:00 ET expiry is left out. One written at
    or after 15:45 ET is flagged late: an approval raised then has minutes to be
    given. Read-only. Placing is the agent's job (docs/robinhood-mcp.md); record
    what came back with 'trd engine outbox ack'.

    The guardrails apply here too: halted, nothing is emitted (`halted: true`);
    a buy that breaks a hard cap, or one sized off a quote more than 30 minutes
    old, is listed under `withheld` with the reason and is never in `orders`.
    """
    if ctx.invoked_subcommand is not None:
        return
    _use_json(as_json)
    db_path = get_settings().db_path
    try:
        if not db_path.exists():
            raise TrdError(f"No database at {db_path}. Run 'trd init' first.")
        conn = connect_read_only(db_path)
        try:
            outbox = OutboxService(conn).pending(datetime.now(), include_stale=include_stale)
        finally:
            conn.close()
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(outbox)
        return
    for renderable in outbox_renderables(outbox):
        console.print(renderable)


@outbox_app.command("ack")
def engine_outbox_ack(
    intent: Annotated[str, typer.Argument(help="Intent id or ref_id, from 'trd engine outbox'.")],
    approval_id: Annotated[
        str | None,
        typer.Option("--approval-id", help="The place call returned a trade approval."),
    ] = None,
    broker_order_id: Annotated[
        str | None,
        typer.Option("--broker-order-id", help="The place call returned a broker order."),
    ] = None,
    error: Annotated[
        str | None,
        typer.Option("--error", help="The place call failed; nothing reached the broker."),
    ] = None,
    notify: Annotated[
        bool,
        typer.Option(
            "--notify",
            help="With --approval-id, ask for the approval in the live alert chat (Telegram).",
        ),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Record what the broker answered when an outbox order was placed.

    --approval-id: pending -> awaiting_approval. --broker-order-id: pending or
    awaiting_approval -> submitted. --error: pending -> rejected. The same ack
    twice changes nothing; an ack against a resolved intent, or with a different
    approval / order id than the one recorded, is refused and exits 1.

    A retry of a failed place reuses the intent's ref_id — never a new one.
    """
    _use_json(as_json)
    settings = get_settings()
    try:
        service = OutboxService(connect(settings.db_path))
        try:
            ack = service.ack(
                intent,
                datetime.now(),
                approval_id=approval_id,
                broker_order_id=broker_order_id,
                error=error,
            )
        finally:
            service.conn.close()
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(ack)
    else:
        verb = (
            f"{ack.from_status.value} -> {ack.to_status.value}"
            if ack.changed
            else f"already {ack.to_status.value} — nothing changed"
        )
        console.print(
            f"Intent #{ack.intent.id} {ack.intent.side.value} [bold]{ack.symbol}[/bold]: {verb}"
        )
    if notify and approval_id and ack.changed:
        _send([approval_message(ack, label_from_env(live=True))], alert=True)


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
        str | None,
        typer.Option(
            "--fill",
            help="Exit fill model: 'intrabar' checks stops/targets against each bar's "
            "range (gaps fill at the open); 'close' only ever fills at the close. "
            "Default: intrabar on daily bars, close on intraday ones — the model that "
            "reproduces the live engine's own stop fills on each.",
        ),
    ] = None,
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
    regime_filter: Annotated[
        bool | None,
        typer.Option(
            "--regime/--no-regime",
            help="Override the market-regime gate for this run — the point is to "
            "compare the same history with it on and off.",
        ),
    ] = None,
    settlement: Annotated[
        bool | None,
        typer.Option(
            "--settlement/--no-settlement",
            help="Override T+1 settled cash for this run: exit proceeds wait a business "
            "day before they can fund an entry. Default: on when the engine has a "
            "capital ('trd engine cash'), off otherwise.",
        ),
    ] = None,
    scale_out: Annotated[
        float | None,
        typer.Option(
            "--scale-out",
            help="Override the scale-out percentage for this run — the point is to "
            "replay the same history closing flat at the target and letting a "
            "runner ride, and compare expectancy in R.",
        ),
    ] = None,
    entry_fill: Annotated[
        str | None,
        typer.Option(
            "--entry-fill",
            help="Entry fill model: 'open' reads the next bar just opened and fills at "
            "its open, as a live scan does; 'close' reads the signal bar complete and "
            "fills at its close. Default: open on intraday bars, close on daily.",
        ),
    ] = None,
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            "-p",
            help="Override one exit param for this run, KEY=VALUE (repeatable) — "
            "e.g. -p trail_atr_mult=2 or -p entry_delay_minutes=10. Sweep one axis "
            "at a time against the same history; the stored config is untouched.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the result as JSON.")] = False,
) -> None:
    """Replay the engine's rules against stored history. Same rules, same
    scorecard as 'trd engine report' — hundreds of trades in one run instead of
    four years of waiting. Needs depth: run 'trd sync --years 10' first."""
    _use_json(as_json)
    db_path = get_settings().db_path
    if not db_path.exists():
        _fail(TrdError(f"No database at {db_path}. Run 'trd init' first."))
        return
    # Read-only (SB-1340): a replay writes nothing, and a writable connection
    # would hold the single-writer lock a live scan needs.
    service = BacktestService(connect_read_only(db_path))
    try:
        fill_mode = FillMode(fill) if fill else None
    except ValueError:
        err_console.print(f"[red]error:[/red] --fill must be 'intrabar' or 'close', not {fill!r}")
        raise typer.Exit(code=1) from None
    try:
        entry_mode = EntryFill(entry_fill) if entry_fill else None
    except ValueError:
        _fail(TrdError(f"--entry-fill must be 'open' or 'close', not {entry_fill!r}"))
        return
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
            regime_filter=regime_filter,
            scale_out_pct=scale_out,
            params_override=_parse_params(param or []),
            entry_fill=entry_mode,
            settlement=settlement,
        )
    except TrdError as exc:
        _fail(exc)
        return
    except duckdb.Error as exc:
        # A database older than this build: read-only cannot migrate it, and on a
        # live engine home it must not be migrated from here anyway — the image
        # still running there would then be writing a schema it predates.
        _fail(
            TrdError(
                f"{str(exc).splitlines()[0]} — this database's schema is older than this "
                "build. Backtest a copy instead, or deploy this build to that engine first."
            )
        )
        return
    if as_json:
        console.print_json(result.model_dump_json())
        return
    for renderable in engine_backtest_renderables(result, width=max(40, console.size.width - 20)):
        console.print(renderable)


@engine_app.command("what-if")
def engine_what_if(
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            "-p",
            help="An exit knob and the values to try, KEY=V1[,V2...] (repeatable). "
            "Each value is its own row; each -p is its own axis against the same "
            "baseline, never a grid.",
        ),
    ] = None,
    start: Annotated[
        str | None, typer.Option("--start", help="First entry date to include (ISO).")
    ] = None,
    end: Annotated[
        str | None, typer.Option("--end", help="Last entry date to include (ISO).")
    ] = None,
    fill: Annotated[
        str | None,
        typer.Option(
            "--fill",
            help="Exit fill model: 'intrabar' or 'close'. Default: the one the backtest "
            "uses on this timeframe.",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Replay different exit rules on the entries the engine really took.

    Every closed live trade keeps its real fill; only the exits are re-run, over
    the bars stored after it. Each variant is scored against a replay of the live
    config on the SAME trades — paired, so a few hundredths of an R is visible
    where a backtest sweep could not see a tenth. Read-only: safe during a session.
    """
    _use_json(as_json)
    try:
        variants = parse_variants(param or [])
        fill_mode = FillMode(fill) if fill else None
    except TrdError as exc:
        _fail(exc)
        return
    except ValueError:
        _fail(TrdError(f"--fill must be 'intrabar' or 'close', not {fill!r}"))
        return
    start_at = _parse_date(start)
    end_at = _parse_date(end)
    try:
        service = ExitWhatIfService(connect_read_only(get_settings().db_path))
        with _spinner("Replaying exits on the live entries..."):
            result = service.run(
                variants,
                start=start_at.date() if start_at else None,
                end=end_at.date() if end_at else None,
                fill=fill_mode,
            )
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(result)
        return
    for renderable in exit_whatif_renderables(result):
        console.print(renderable)


@engine_app.command("fidelity")
def engine_fidelity(
    days: Annotated[
        int, typer.Option("--days", "-d", help="Trailing window of live entries, in days.")
    ] = 30,
    start: Annotated[
        str | None, typer.Option("--start", help="First entry date (ISO). Overrides --days.")
    ] = None,
    end: Annotated[str | None, typer.Option("--end", help="Last entry date (ISO).")] = None,
    entry_fill: Annotated[
        str | None,
        typer.Option(
            "--entry-fill",
            help="Entry fill model: 'open' reads the next bar just opened and fills at "
            "its open, as a live scan does; 'close' reads the signal bar complete and "
            "fills at its close. Default: open on intraday bars, close on daily.",
        ),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Does the backtest trade the way the engine did? Replays the window and lays
    it against the live book: trades only one side took, and on the trades both
    took, how much of the difference is the entry and how much the exit.

    Read before believing any backtest. Read-only, so it is safe mid-session.
    """
    _use_json(as_json)
    start_at = _parse_date(start)
    end_at = _parse_date(end)
    try:
        service = FidelityService(connect_read_only(get_settings().db_path))
        with _spinner("Replaying the window and pairing it with the live trades..."):
            result = service.run(
                start=start_at.date() if start_at else window_start(days),
                end=end_at.date() if end_at else None,
                entry_fill=EntryFill(entry_fill) if entry_fill else None,
            )
    except (TrdError, ValueError) as exc:
        _fail(exc if isinstance(exc, TrdError) else TrdError(f"--entry-fill: {exc}"))
        return
    if as_json:
        _emit_json(result)
        return
    for renderable in fidelity_renderables(result):
        console.print(renderable)


@engine_app.command("sweep")
def engine_sweep(
    param: Annotated[
        list[str] | None,
        typer.Option(
            "--param",
            "-p",
            help="A knob and the values to try, KEY=V1[,V2...] (repeatable). Each value "
            "is its own row; each -p its own axis against the same baseline.",
        ),
    ] = None,
    start: Annotated[str | None, typer.Option("--start", help="First date (ISO).")] = None,
    end: Annotated[str | None, typer.Option("--end", help="Last date (ISO).")] = None,
    workers: Annotated[
        int | None, typer.Option("--workers", help="Parallel replays. Default: cores - 2, max 6.")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Sweep backtest params one axis at a time, with the error bars on: each row's
    expectancy ± SE, its difference from the baseline, drawdown, capture, and
    whether both halves of the window agree. For knobs that change which trades
    exist; exit-only knobs measure tighter with 'trd engine what-if'. Read-only.
    """
    _use_json(as_json)
    try:
        variants = parse_axes(param or [])
    except TrdError as exc:
        _fail(exc)
        return
    db_path = get_settings().db_path
    if not db_path.exists():
        _fail(TrdError(f"No database at {db_path}. Run 'trd init' first."))
        return
    start_at = _parse_date(start)
    end_at = _parse_date(end)
    try:
        with _spinner(f"Replaying the baseline and {len(variants)} variant(s)..."):
            result = sweep(
                db_path,
                variants,
                start=start_at.date() if start_at else None,
                end=end_at.date() if end_at else None,
                workers=workers,
            )
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(result)
        return
    for renderable in sweep_renderables(result):
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


@engine_app.command("trim")
def engine_trim(
    symbol: Annotated[str, typer.Argument(help="Symbol the engine is holding.")],
    pct: Annotated[
        float | None,
        typer.Option("--pct", help="Percent of what is still held to sell, e.g. 50."),
    ] = None,
    quantity: Annotated[
        str | None, typer.Option("--quantity", help="Exact quantity to sell instead of a percent.")
    ] = None,
    price: Annotated[
        str | None, typer.Option("--price", help="Fill price. Omit to use the live quote.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the fill as JSON.")] = False,
) -> None:
    """Sell part of an open engine position and leave the rest running.

    For taking cash out of a live trade without abandoning it. The stop, target
    and trail are untouched — trimming changes the size, not the plan. Use an
    exit rule to close a position outright, so the trade records why it ended.
    """
    _use_json(as_json)
    service = _engine_service()
    try:
        fill = service.trim(
            symbol,
            pct=Decimal(str(pct)) if pct is not None else None,
            quantity=_parse_decimal(quantity, "quantity") if quantity else None,
            price=_parse_decimal(price, "price") if price else None,
        )
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        console.print_json(fill.model_dump_json())
        return
    console.print(
        f"Trimmed [bold]{fill.quantity.normalize():f}[/bold] {fill.symbol} @ "
        f"{fmt_money(fill.price)} ({fmt_signed(fill.pnl)} booked).\n{fill.reason}"
    )


@engine_app.command("rules")
def engine_rules() -> None:
    """What every entry strategy looks for and how each exit rule works."""
    service = _engine_service()
    params, timeframe = None, DAILY
    with suppress(TrdError):  # rules are readable before the engine is configured
        config = service.config()
        params, timeframe = config.exit_params, config.timeframe
    console.print(engine_strategies_table(timeframe))
    console.print(engine_exits_table(params, timeframe))


@bot_app.command("serve")
def bot_serve(
    passes: Annotated[
        int, typer.Option("--passes", help="Stop after N polls. 0 = run until killed.")
    ] = 0,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Seconds Telegram holds an idle poll open.")
    ] = POLL_TIMEOUT,
) -> None:
    """Run the Telegram command bot: poll for messages, queue what they ask for.

    Long-polls rather than serving a webhook, so it needs no public endpoint and
    no certificate — only outbound 443. Exactly one of these may run per bot
    token; a second gets a permanent 409 from Telegram.

    Reads TELEGRAM_BOT_TOKEN, TRD_BOT_ENGINES ('swing=/path,day=/path') and
    TRD_BOT_ALLOWED_USER_IDS (numeric Telegram user ids). Opens no database.
    """
    try:
        bot = bot_from_env()
    except BotConfigError as exc:
        _fail(exc)
        return
    names = ", ".join(f"{e.name}→{e.home}" for e in bot.engines)
    console.print(f"bot listening · engines: {names} · authorized: {len(bot.allowed_user_ids)}")
    try:
        bot.serve(passes=passes or None, timeout=timeout)
    except KeyboardInterrupt:
        console.print("stopped")
    except TrdError as exc:
        _fail(exc)


@bot_app.command("check")
def bot_check() -> None:
    """Validate the bot's configuration without polling.

    Deploy verification: it answers "is this pod going to work" without taking
    the token's single poll slot, which a running bot already holds.
    """
    try:
        bot = bot_from_env()
    except BotConfigError as exc:
        _fail(exc)
        return
    console.print(f"authorized users: {len(bot.allowed_user_ids)}")
    console.print(f"offset file: {bot.offset_path} (at {bot.offset()})")
    for engine in bot.engines:
        db = engine.home / "trd.duckdb"
        published = (engine.home / "status.json").exists()
        state = "ok" if db.exists() else "no database at this path"
        console.print(
            f"  {engine.name}: {engine.home} — {state}"
            f"{'' if published else ', no snapshot published yet'}"
        )


# ------------------------------------------------------------- cash and alloc

cash_app = typer.Typer(
    help="Money moved into or out of an account: deposits and withdrawals.",
    no_args_is_help=True,
)
app.add_typer(cash_app, name="cash")
alloc_app = typer.Typer(
    help="A staged allocation plan on an account: targets, schedule, drift, proposals.",
    no_args_is_help=True,
)
app.add_typer(alloc_app, name="alloc")
milestone_app = typer.Typer(help="Postpone, cancel or annotate a deployment milestone.")
alloc_app.add_typer(milestone_app, name="milestone")

AllocAccountOpt = Annotated[
    str | None,
    typer.Option("--account", "-a", help="Account with the plan (default: the only plan)."),
]
TargetOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--target",
        "-t",
        help="SYMBOL=WEIGHT, repeated; weights sum to 100. CASH=5 holds 5% uninvested.",
    ),
]
PaceOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--pace",
        help="SYMBOL=MONTHS: buy that target's share of the capital in monthly instalments "
        "over MONTHS, outside the tranches (SPCX=24). SYMBOL=0 un-paces it.",
    ),
]
PaceStartOpt = Annotated[
    str | None,
    typer.Option(
        "--pace-start", help="First instalment date (default: the schedule's start, else today)."
    ),
]
CashFundOpt = Annotated[
    str | None,
    typer.Option(
        "--cash-fund", help="Core money-market fund the uninvested cash sits in, e.g. SPAXX."
    ),
]
CashYieldOpt = Annotated[
    str | None,
    typer.Option("--cash-yield", help="That fund's yield, annual percent (e.g. 3.51 = 3.51%)."),
]
CashYieldAsOfOpt = Annotated[
    str | None,
    typer.Option("--cash-yield-as-of", help="Date the yield was read (default: today)."),
]


def _cash_yield_args(
    current: CashYield | None, fund: str | None, pct: str | None, as_of: str | None
) -> CashYield | None:
    when = _parse_date(as_of)
    return resolve_cash_yield(
        current,
        fund,
        _parse_decimal(pct, "cash yield") if pct is not None else None,
        when.date() if when else None,
        date.today(),
    )


def _cash_service() -> CashService:
    return CashService(connect(get_settings().db_path))


def _alloc_service() -> AllocService:
    return AllocService(connect(get_settings().db_path), YFinanceProvider())


def _parse_targets(raw: list[str] | None) -> list[AllocTarget] | None:
    if not raw:
        return None
    out = []
    for item in raw:
        if "=" not in item:
            _fail(TrdError(f"--target looks like SYMBOL=WEIGHT, got {item!r}"))
        symbol, weight = item.split("=", 1)
        out.append(
            AllocTarget(symbol=symbol.strip().upper(), weight=_parse_decimal(weight, "weight"))
        )
    return out


def _apply_pace(
    targets: list[AllocTarget], raw: list[str] | None, start: date
) -> list[AllocTarget]:
    """Mark targets as paced: SYMBOL=MONTHS, or SYMBOL=0 to make one ordinary again."""
    by_symbol = {t.symbol: t for t in targets}
    for item in raw or []:
        if "=" not in item:
            _fail(TrdError(f"--pace looks like SYMBOL=MONTHS, got {item!r}"))
        symbol, months = item.split("=", 1)
        symbol = symbol.strip().upper()
        if symbol not in by_symbol:
            _fail(TrdError(f"--pace {symbol}: not a target in this plan."))
        try:
            n = int(months)
        except ValueError:
            _fail(TrdError(f"--pace {symbol}: months must be a whole number, got {months!r}"))
            raise
        t = by_symbol[symbol]
        by_symbol[symbol] = t.model_copy(
            update={"pace_months": n or None, "pace_start": start if n else None}
        )
    return [by_symbol[t.symbol] for t in targets]


def _cash_move(
    kind: CashFlowKind, amount: str, account: str, date_str: str | None, note: str | None
) -> None:
    when = _parse_date(date_str) or datetime.now()
    try:
        flow = _cash_service().record(account, kind, _parse_decimal(amount, "amount"), when, note)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(
        f"Recorded {flow.kind.value} {fmt_money(flow.amount)} in [bold]{account}[/bold] "
        f"on {flow.occurred_at:%Y-%m-%d}."
    )


@cash_app.command("deposit")
def cash_deposit(
    amount: Annotated[str, typer.Argument(help="Amount that arrived, e.g. 250930.")],
    account: Annotated[str, typer.Option("--account", "-a", help="Account it landed in.")],
    date_str: Annotated[
        str | None, typer.Option("--date", "-d", help="When it landed (YYYY-MM-DD). Default: now.")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="Where it came from.")] = None,
) -> None:
    """Record money arriving in an account — a rollover, a contribution, a transfer in.

    Not a buy: it creates no shares. It is what makes the account's cash, its
    value and its return honest — without it, money waiting to be invested is
    invisible and every return starts at the first buy instead of the deposit.
    """
    _cash_move(CashFlowKind.DEPOSIT, amount, account, date_str, note)


@cash_app.command("withdraw")
def cash_withdraw(
    amount: Annotated[str, typer.Argument(help="Amount that left, e.g. 1000.")],
    account: Annotated[str, typer.Option("--account", "-a", help="Account it left.")],
    date_str: Annotated[
        str | None, typer.Option("--date", "-d", help="When (YYYY-MM-DD). Default: now.")
    ] = None,
    note: Annotated[str | None, typer.Option("--note", help="Why.")] = None,
) -> None:
    """Record money leaving an account — a distribution, a transfer out."""
    _cash_move(CashFlowKind.WITHDRAWAL, amount, account, date_str, note)


@cash_app.command("ls")
def cash_ls(
    account: Annotated[str, typer.Option("--account", "-a", help="Account.")],
    as_json: JsonOpt = False,
) -> None:
    """Every deposit and withdrawal, and the cash balance they imply:
    deposits - withdrawals - buys + sells + income."""
    _use_json(as_json)
    service = _cash_service()
    try:
        flows, balance = service.flows_for(account), service.balance(account)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(
            {
                "flows": [f.model_dump(mode="json") for f in flows],
                "balance": balance.model_dump(mode="json"),
            }
        )
        return
    console.print(cash_flows_table(flows, balance))


@alloc_app.command("set")
def alloc_set(
    account: Annotated[str, typer.Option("--account", "-a", help="Existing account.")],
    capital: Annotated[str, typer.Option("--capital", help="What the plan invests in total.")],
    target: TargetOpt = None,
    pace: PaceOpt = None,
    pace_start: PaceStartOpt = None,
    name: Annotated[str | None, typer.Option("--name", help="Plan name.")] = None,
    kind: Annotated[
        str | None, typer.Option("--kind", help="Account kind, e.g. 'Rollover IRA'.")
    ] = None,
    brokerage: Annotated[str | None, typer.Option("--brokerage", help="e.g. Fidelity.")] = None,
    drift: Annotated[
        str, typer.Option("--drift", help="Drift alert threshold, percentage points.")
    ] = "5",
    note: Annotated[str | None, typer.Option("--note", help="Strategy, in your words.")] = None,
    cash_fund: CashFundOpt = None,
    cash_yield: CashYieldOpt = None,
    cash_yield_as_of: CashYieldAsOfOpt = None,
) -> None:
    """Create the allocation plan for an account: capital and target weights.

    A plan, never an order: trd records what you buy at the broker and measures
    the account against these targets. --cash-fund/--cash-yield state what the
    uninvested cash earns while it waits.
    """
    targets = _parse_targets(target)
    if not targets:
        _fail(TrdError("A plan needs --target SYMBOL=WEIGHT entries."))
        return
    started = _parse_date(pace_start)
    targets = _apply_pace(targets, pace, started.date() if started else date.today())
    try:
        plan = _alloc_service().create(
            account,
            name or f"{account} plan",
            _parse_decimal(capital, "capital"),
            targets,
            account_kind=kind,
            brokerage=brokerage,
            drift_threshold=_parse_decimal(drift, "drift"),
            note=note,
            cash_yield=_cash_yield_args(None, cash_fund, cash_yield, cash_yield_as_of),
        )
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Plan [bold]{plan.name}[/bold] on {account}: {fmt_money(plan.capital)}.")
    console.print("Next: trd alloc schedule --start YYYY-MM-DD --initial ... --amount ... ")


@alloc_app.command("edit")
def alloc_edit(
    account: AllocAccountOpt = None,
    target: TargetOpt = None,
    pace: PaceOpt = None,
    pace_start: PaceStartOpt = None,
    capital: Annotated[str | None, typer.Option("--capital")] = None,
    drift: Annotated[str | None, typer.Option("--drift")] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    cash_fund: CashFundOpt = None,
    cash_yield: CashYieldOpt = None,
    cash_yield_as_of: CashYieldAsOfOpt = None,
    no_cash_yield: Annotated[
        bool, typer.Option("--no-cash-yield", help="Forget the stated cash yield.")
    ] = False,
) -> None:
    """Change the targets (all of them, summing to 100), pace, capital, drift
    threshold, name or cash yield. Every change is kept in 'trd alloc history'.

    --target replaces every target, so restate --pace with it; --pace alone
    re-paces the targets the plan already has. --cash-yield alone updates the
    rate on the fund already stated, dated today unless --cash-yield-as-of."""
    service = _alloc_service()
    try:
        name_ = account or service.default_account()
        targets = _parse_targets(target)
        if pace:
            current = service.get(name_)
            started = _parse_date(pace_start)
            first = min((m.due_date for m in current.milestones if m.seq == 0), default=None)
            start = started.date() if started else first or date.today()
            targets = _apply_pace(targets or current.targets, pace, start)
        new_yield = None
        if cash_fund is not None or cash_yield is not None or cash_yield_as_of is not None:
            new_yield = _cash_yield_args(
                service.get(name_).cash_yield, cash_fund, cash_yield, cash_yield_as_of
            )
        plan = service.edit(
            name_,
            targets=targets,
            capital=_parse_decimal(capital, "capital") if capital else None,
            drift_threshold=_parse_decimal(drift, "drift") if drift else None,
            name=name,
            note=note,
            cash_yield=new_yield,
            clear_cash_yield=no_cash_yield,
        )
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Updated [bold]{plan.name}[/bold].")


@alloc_app.command("schedule")
def alloc_schedule(
    start: Annotated[str, typer.Option("--start", help="Date of the initial tranche.")],
    initial: Annotated[str, typer.Option("--initial", help="Amount invested at the start.")],
    amount: Annotated[str, typer.Option("--amount", help="Each later tranche.")] = "0",
    every: Annotated[int, typer.Option("--every", help="Months between tranches.")] = 3,
    count: Annotated[int, typer.Option("--count", help="Number of later tranches.")] = 0,
    account: AllocAccountOpt = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Rebuild an existing schedule.")
    ] = False,
) -> None:
    """Build the deployment schedule: an initial tranche, then COUNT tranches of
    AMOUNT every EVERY months. Must add up to the plan's capital exactly."""
    service = _alloc_service()
    when = _parse_date(start)
    assert when is not None
    try:
        name_ = account or service.default_account()
        plan = service.schedule(
            name_,
            when.date(),
            _parse_decimal(initial, "initial"),
            _parse_decimal(amount, "amount"),
            every,
            count,
            replace=replace,
        )
    except TrdError as exc:
        _fail(exc)
        return
    for m in plan.milestones:
        label = "initial" if m.seq == 0 else f"#{m.seq}"
        console.print(f"  {label:>7}  {m.due_date:%Y-%m-%d}  {fmt_money(m.amount)}")


@alloc_app.command("show")
def alloc_show(account: AllocAccountOpt = None, as_json: JsonOpt = False) -> None:
    """The plan against the account: value, cash, return, allocation and drift,
    and the schedule — each milestone's state read from what you actually bought."""
    _use_json(as_json)
    service = _alloc_service()
    try:
        name_ = account or service.default_account()
        with _spinner("Fetching quotes..."):
            view = service.view(name_)
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(view)
        return
    for renderable in alloc_view_renderables(view):
        console.print(renderable)


@alloc_app.command("propose")
def alloc_propose(
    account: AllocAccountOpt = None,
    amount: Annotated[
        str | None, typer.Option("--amount", help="Amount to deploy (default: next milestone).")
    ] = None,
    milestone: Annotated[
        int | None,
        typer.Option("--milestone", "-m", help="Milestone to propose for (0 = initial)."),
    ] = None,
    mode: Annotated[
        str, typer.Option("--mode", help="underweight (default) or target.")
    ] = "underweight",
    set_: Annotated[
        list[str] | None,
        typer.Option("--set", help="Fix a fund's amount: --set IVV=60000; the rest re-splits."),
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """How a deployment could be split across the plan. A suggestion to review and
    edit — never an order, and nothing is recorded."""
    _use_json(as_json)
    service = _alloc_service()
    overrides: dict[str, Decimal] = {}
    for item in set_ or []:
        if "=" not in item:
            _fail(TrdError(f"--set looks like SYMBOL=AMOUNT, got {item!r}"))
        symbol, value = item.split("=", 1)
        overrides[symbol.strip().upper()] = _parse_decimal(value, "amount")
    try:
        name_ = account or service.default_account()
        with _spinner("Fetching quotes..."):
            proposal = service.proposal(
                name_,
                amount=_parse_decimal(amount, "amount") if amount else None,
                seq=milestone,
                mode=mode,
                overrides=overrides,
            )
    except TrdError as exc:
        _fail(exc)
        return
    if as_json:
        _emit_json(proposal)
        return
    for renderable in proposal_renderables(proposal):
        console.print(renderable)


@alloc_app.command("report")
def alloc_report(
    account: AllocAccountOpt = None,
    notify: Annotated[
        bool, typer.Option("--notify", help="Send it to your private Telegram chat.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """The daily performance message: a verdict on the day, worth and return
    since funding, how your funds compare with the S&P 500 today and since you
    started (the same dollars on the same days), the Nasdaq, world stocks and a
    stocks+bonds fund today, the funds best first, cash and what it earns, drift
    past the threshold, and the next milestone. --json also carries the whole
    account and your target mix scored the same way.

    READ-ONLY: opens the database without write access and stores no quotes, so
    an unattended run (deploy/alloc-report.sh, launchd) can never become a second
    writer on a database that syncs through iCloud. --notify goes to the private
    alert chat (TRD_LIVE_ALERT_CHAT_ID, else the first TRD_BOT_ALLOWED_USER_IDS) —
    personal money, never the channel.
    """
    _use_json(as_json)
    try:
        service = AllocService(
            connect_read_only(get_settings().db_path), YFinanceProvider(), snapshot=False
        )
        name_ = account or service.default_account()
        with _spinner("Fetching quotes..."):
            report = AllocReportService(service).report(name_)
    except TrdError as exc:
        _fail(exc)
        return
    text = alloc_report_message(report)
    if notify:
        refused: list[str] = []
        notifier = alert_notifier_from_env(error=refused.append)
        if notifier is None:
            _fail(TrdError(refused[0] if refused else "No TELEGRAM_BOT_TOKEN set."))
            return
        try:
            notifier.send(text, html=True)
        except TrdError as exc:  # NotifyError: delivery failed, token never shown
            _fail(exc)
            return
    if as_json:
        _emit_json(
            {
                "message": text,
                "sent": notify,
                "view": report.view.model_dump(mode="json"),
                "report": report.model_dump(mode="json", exclude={"view"}),
            }
        )
        return
    console.print(strip_html(text), markup=False, highlight=False)


@alloc_app.command("history")
def alloc_history(account: AllocAccountOpt = None) -> None:
    """Every material change to the plan, oldest first."""
    service = _alloc_service()
    try:
        events = service.history(account or service.default_account())
    except TrdError as exc:
        _fail(exc)
        return
    for e in events:
        console.print(f"{e.at:%Y-%m-%d %H:%M}  {e.what}")


MilestoneSeq = Annotated[int, typer.Argument(help="Milestone number (0 = initial).")]


@milestone_app.command("postpone")
def milestone_postpone(
    seq: MilestoneSeq,
    to: Annotated[str, typer.Option("--to", help="New due date (YYYY-MM-DD).")],
    note: Annotated[str | None, typer.Option("--note", help="Why — kept in the history.")] = None,
    account: AllocAccountOpt = None,
) -> None:
    """Move a milestone to a later date."""
    service = _alloc_service()
    new = _parse_date(to)
    assert new is not None
    try:
        service.postpone(account or service.default_account(), seq, new.date(), note)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Milestone {seq} → {new:%Y-%m-%d}.")


@milestone_app.command("cancel")
def milestone_cancel(
    seq: MilestoneSeq,
    note: Annotated[str | None, typer.Option("--note")] = None,
    account: AllocAccountOpt = None,
) -> None:
    """Drop a milestone from the schedule (its money stays as cash)."""
    service = _alloc_service()
    try:
        service.cancel(account or service.default_account(), seq, note)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Milestone {seq} cancelled.")


@milestone_app.command("resume")
def milestone_resume(seq: MilestoneSeq, account: AllocAccountOpt = None) -> None:
    """Put a postponed or cancelled milestone back to planned."""
    service = _alloc_service()
    try:
        service.resume(account or service.default_account(), seq)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Milestone {seq} planned.")


@milestone_app.command("note")
def milestone_note(
    seq: MilestoneSeq,
    text: Annotated[str, typer.Argument(help="Decision rationale, market conditions...")],
    account: AllocAccountOpt = None,
) -> None:
    """Attach a note to a milestone."""
    service = _alloc_service()
    try:
        service.annotate(account or service.default_account(), seq, text)
    except TrdError as exc:
        _fail(exc)
        return
    console.print(f"Noted on milestone {seq}.")


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
