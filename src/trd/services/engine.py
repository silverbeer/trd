"""The trading engine.

One `scan()` is the whole loop: refresh quotes, walk every open position through
the exit rules, then run the entry strategies over the universe and take the
best-ranked candidates that still fit inside the position limit.

Fills are recorded as ordinary txn rows in a *simulation* account, so everything
trd already knows how to do — FIFO, portfolio, equity curve, XIRR, drawdown —
works on the engine account without a line of new code. `engine_position` holds
only what a transaction cannot express: which rule fired, why, and where the
stops sit.

Signals are computed on stored daily bars with the live quote folded in as the
forming bar, so the math is the same daily math the indicator panel shows, but
the engine can react intraday.
"""

from collections.abc import Sequence
from datetime import date, datetime
from decimal import ROUND_DOWN, Decimal

import duckdb
from pydantic import BaseModel

from trd.build import build_version
from trd.engine import DEFAULT_EXIT_PARAMS, EXIT_REGISTRY, evaluate_exits, missing_rules
from trd.engine import REGISTRY as STRATEGIES
from trd.engine.base import indicator, last
from trd.errors import ProviderError, TrdError
from trd.models import (
    Account,
    AccountType,
    DailyBar,
    EngineConfig,
    EnginePosition,
    EngineRun,
    EngineStatus,
    Instrument,
    PositionRow,
    Quote,
    Side,
    SignalRow,
    StrategyStat,
)
from trd.providers.base import MarketDataProvider
from trd.repos import (
    AccountRepo,
    EarningsRepo,
    InstrumentRepo,
    PriceRepo,
    TransactionRepo,
    WatchlistRepo,
)
from trd.repos.engine import (
    EngineConfigRepo,
    EnginePositionRepo,
    EngineRunRepo,
    EngineSignalRepo,
)

DEFAULT_ENGINE_ACCOUNT = "engine-sim"
DEFAULT_ENGINE_WATCHLIST = "engine"
DEFAULT_POSITION_SIZE = Decimal("1000")
DEFAULT_MAX_POSITIONS = 5
# No new entry when a print lands within this many days. Three sessions is enough
# to keep a fresh trade out of a binary event without blacking out half the month.
DEFAULT_EARNINGS_BLACKOUT_DAYS = 3
# How long before a day-mode engine's flat time it stops taking new entries.
ENTRY_CUTOFF_MINUTES = 30

# Ten liquid, heavily-covered names. Small enough to reason about by hand, which
# is the point of a monitor-mode dry run — you should be able to check the
# engine's homework on every trade it takes.
DEFAULT_UNIVERSE = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "AVGO",
    "AMD",
    "TSLA",
    "NFLX",
]


class ScanSignal(BaseModel):
    symbol: str
    strategy: str
    score: float
    reason: str
    price: Decimal
    acted: bool = False


class ScanFill(BaseModel):
    symbol: str
    strategy: str
    quantity: Decimal
    price: Decimal
    reason: str
    rule: str | None = None
    pnl: Decimal | None = None
    r_multiple: Decimal | None = None


class ScanResult(BaseModel):
    run_id: int
    at: datetime
    paper: bool
    scanned: int
    signals: list[ScanSignal] = []
    opened: list[ScanFill] = []
    closed: list[ScanFill] = []
    skipped: list[str] = []
    open_positions: int = 0
    capacity: int = 0

    @property
    def quiet(self) -> bool:
        return not self.signals and not self.opened and not self.closed


class EntryPlan(BaseModel):
    """A sized fill at the last bar's close: how much, and where the exits sit."""

    quantity: Decimal
    stop_price: Decimal
    target_price: Decimal
    atr: Decimal


def plan_entry(
    bars: list[DailyBar], position_size: Decimal, exit_params: dict[str, float]
) -> tuple[EntryPlan | None, str | None]:
    """Quantity, initial stop and target for an entry at the last bar's close.

    Shared by the live engine and the backtest so their fills cannot drift apart.
    Returns (plan, skip_reason); both None when the numbers refuse without a story
    worth telling (non-positive price, or a stop that would sit below zero).

    Fractional quantity, because flooring to whole shares silently un-fixes
    fixed-dollar sizing: a $340 name in a $1000 slot gets $680 of exposure and a
    $1278 name gets none at all, so price alone re-weights the book and quietly
    drops the expensive half of the universe. 6dp matches broker fill precision
    and sits well inside the DECIMAL(24, 8) the txn and position tables store.
    """
    price = bars[-1].close
    if price <= 0:
        return None, None
    quantity = (position_size / price).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    if quantity <= 0:
        return None, (
            f"{price:.2f}/share against a {position_size:.0f} position size rounds to nothing"
        )
    atr = last(indicator("atr", bars, period=14)["value"])
    if atr is None or atr <= 0:
        return None, "no ATR yet — cannot size the stop"
    atr_dec = Decimal(str(atr))
    stop = price - atr_dec * Decimal(str(exit_params.get("stop_atr_mult", 2.0)))
    if stop <= 0:
        return None, None
    target = price + (price - stop) * Decimal(str(exit_params.get("target_r", 2.0)))
    return EntryPlan(quantity=quantity, stop_price=stop, target_price=target, atr=atr_dec), None


def strategy_stats(
    closed: list[EnginePosition], open_counts: dict[str, int] | None = None
) -> list[StrategyStat]:
    """Group closed trades into the per-strategy scorecard. Shared by the live
    report and the backtest so the two read on the same scale."""
    open_counts = open_counts or {}
    grouped: dict[str, list[EnginePosition]] = {}
    for position in closed:
        grouped.setdefault(position.strategy, []).append(position)

    stats: list[StrategyStat] = []
    for key in sorted(set(grouped) | set(open_counts)):
        trades = grouped.get(key, [])
        wins = [p for p in trades if (p.realized_pnl or Decimal(0)) > 0]
        losses = [p for p in trades if (p.realized_pnl or Decimal(0)) <= 0]
        win_pcts = [p.pnl_pct_at(p.exit_price) for p in wins]
        loss_pcts = [p.pnl_pct_at(p.exit_price) for p in losses]
        rs = [p.realized_r for p in trades if p.realized_r is not None]
        stats.append(
            StrategyStat(
                strategy=key,
                trades=len(trades),
                wins=len(wins),
                losses=len(losses),
                total_pnl=sum((p.realized_pnl or Decimal(0) for p in trades), Decimal(0)),
                avg_win_pct=_mean(win_pcts),
                avg_loss_pct=_mean(loss_pcts),
                avg_r=_mean(rs),
                open_trades=open_counts.get(key, 0),
            )
        )
    return stats


class EngineService:
    def __init__(self, conn: duckdb.DuckDBPyConnection, provider: MarketDataProvider) -> None:
        self.conn = conn
        self.provider = provider
        self.accounts = AccountRepo(conn)
        self.instruments = InstrumentRepo(conn)
        self.prices = PriceRepo(conn)
        self.txns = TransactionRepo(conn)
        self.watchlists = WatchlistRepo(conn)
        self.earnings = EarningsRepo(conn)
        self.configs = EngineConfigRepo(conn)
        self.runs = EngineRunRepo(conn)
        self.signals = EngineSignalRepo(conn)
        self.positions = EnginePositionRepo(conn)

    # ---------------------------------------------------------------- setup

    def init(
        self,
        account_name: str = DEFAULT_ENGINE_ACCOUNT,
        watchlist: str = DEFAULT_ENGINE_WATCHLIST,
        position_size: Decimal = DEFAULT_POSITION_SIZE,
        max_positions: int = DEFAULT_MAX_POSITIONS,
        symbols: list[str] | None = None,
        strategies: list[str] | None = None,
        exit_params: dict[str, float] | None = None,
        earnings_blackout_days: int = DEFAULT_EARNINGS_BLACKOUT_DAYS,
    ) -> tuple[EngineConfig, Account, list[str]]:
        """Create (or re-point) the engine: a simulation account, a watchlist
        universe, and the rule set. Returns the symbols now in the universe."""
        if position_size <= 0:
            raise TrdError("Position size must be positive.")
        if earnings_blackout_days < 0:
            raise TrdError("Earnings blackout days cannot be negative.")
        if max_positions < 1:
            raise TrdError("Max positions must be at least 1.")

        keys = strategies if strategies is not None else sorted(STRATEGIES)
        unknown = [k for k in keys if k not in STRATEGIES]
        if unknown:
            known = ", ".join(sorted(STRATEGIES))
            raise TrdError(f"Unknown strategies: {', '.join(unknown)}. Available: {known}")
        if not keys:
            raise TrdError("Enable at least one strategy.")

        params = {**DEFAULT_EXIT_PARAMS, **(exit_params or {})}
        bad = sorted(set(params) - set(DEFAULT_EXIT_PARAMS))
        if bad:
            raise TrdError(
                f"Unknown exit params: {', '.join(bad)}. "
                f"Available: {', '.join(sorted(DEFAULT_EXIT_PARAMS))}"
            )

        existing = self.accounts.get_by_name(account_name)
        if existing is not None and existing.type != AccountType.SIMULATION:
            raise TrdError(
                f"Account '{account_name}' is a real account. The engine only trades "
                "simulation accounts — pick another name."
            )
        account = existing or self.accounts.create(account_name, AccountType.SIMULATION)

        board = self.watchlists.get_or_create(watchlist)
        for symbol in symbols if symbols is not None else DEFAULT_UNIVERSE:
            instrument = self.instruments.get_by_symbol(symbol)
            if instrument is None:
                instrument = self.instruments.insert(self.provider.get_info(symbol))
            self.watchlists.add_item(board.id, instrument.id)

        config = self.configs.upsert(
            account.id,
            watchlist,
            position_size,
            max_positions,
            keys,
            params,
            earnings_blackout_days,
        )
        universe = [i.symbol for _, i in self.watchlists.items(board.id)]
        return config, account, universe

    def config(self) -> EngineConfig:
        config = self.configs.get()
        if config is None:
            raise TrdError("No engine configured. Run 'trd engine init' first.")
        return config

    @staticmethod
    def _verify_rules(config: EngineConfig) -> None:
        """Refuse to trade a rule set this build cannot honour.

        A stored config can outlive the code that understands it, and the failure
        is silent in the worst direction: a day engine whose `session_close` rule
        is missing does not error, it just carries positions overnight — exactly
        the risk the setting exists to forbid. Failing every scan with a loud
        message is the better outcome, because a stopped engine gets noticed and
        a quietly wrong one does not.
        """
        gaps = missing_rules(config.exit_params)
        if not gaps:
            return
        detail = ", ".join(f"{param} needs the '{rule}' rule" for param, rule in gaps)
        raise TrdError(
            f"This build cannot honour the configured rule set ({detail}). "
            f"Running {build_version()}, whose exit rules are: "
            f"{', '.join(sorted(EXIT_REGISTRY))}. The database was configured by a "
            "newer build than the one executing — redeploy before trading again."
        )

    def account(self) -> Account:
        account = next(
            (a for a in self.accounts.list_all() if a.id == self.config().account_id), None
        )
        if account is None:
            raise TrdError("Engine account is missing. Run 'trd engine init' again.")
        return account

    def universe(self) -> list[Instrument]:
        board = self.watchlists.get_by_name(self.config().watchlist)
        if board is None:
            return []
        return [instrument for _, instrument in self.watchlists.items(board.id)]

    # ------------------------------------------------------------ bar plumbing

    def _stored_bars(self, instrument_id: int) -> list[DailyBar]:
        return self.prices.daily_bars(instrument_id)

    @staticmethod
    def _entry_cutoff(params: dict[str, float]) -> int | None:
        """Minute-of-day after which a day-mode engine stops opening trades.

        Returns None when `flat_at_minute` is off, which is every swing engine.
        Minute-of-day, not HHMM: 15:55 minus 30 minutes is 15:25, and HHMM
        arithmetic would say 15:25 too only by luck — 16:05 minus 30 would give
        15:75.
        """
        flat_at = int(params.get("flat_at_minute", 0))
        if flat_at <= 0:
            return None
        return (flat_at // 100) * 60 + (flat_at % 100) - ENTRY_CUTOFF_MINUTES

    def _earnings_blackout(self, instrument_id: int, today: date, days: int) -> date | None:
        """The earnings date blocking a new entry, or None if the way is clear.

        A 2 x ATR stop bounds the loss at 1R only while price moves continuously.
        An overnight gap skips the level entirely — `StopLoss` compares price to
        the stop, so the exit fills at the next morning's price, not at the stop.
        A trade the engine believes risks 1R can realise several, and because
        `report` averages R-multiples the whole scorecard then describes a risk
        profile the engine is not actually running.

        Entries *after* a print stay allowed on purpose: the gap-and-volume day is
        exactly what `breakout` exists to catch. This removes the coin flip, not
        the setup it creates.
        """
        if days <= 0:
            return None
        next_date = self.earnings.next_for_instrument(instrument_id, today)
        if next_date is None:
            return None
        return next_date if (next_date - today).days <= days else None

    @staticmethod
    def _quote_is_stale(bars: list[DailyBar], quote: Quote | None, today: date) -> bool:
        """True when the quote carries nothing the last settled bar didn't already say.

        A symbol that has not printed yet still answers a quote request — yfinance
        hands back the prior close as `last_price`. Folded in by `_with_live_bar`
        that becomes a forming bar whose open, high, low and close are all
        yesterday's close, indistinguishable from a real flat day. An entry taken
        on it fills at a price that never traded, and because the initial stop is
        immutable the whole trade keeps that fictional basis for life.

        Only applies while today's bar is still synthetic. Once a real bar for
        today exists the quote is refining known-good data, not inventing it.
        """
        if quote is None:
            return True
        if not bars:
            return True
        if bars[-1].date == today:
            return False
        return quote.price == bars[-1].close

    @staticmethod
    def _with_live_bar(bars: list[DailyBar], quote: Quote | None, today: date) -> list[DailyBar]:
        """Fold the live quote into the series as today's forming bar. Daily math,
        intraday reaction — the same bar the close will become."""
        if quote is None:
            return bars
        price = quote.price
        if bars and bars[-1].date == today:
            bar = bars[-1]
            return [
                *bars[:-1],
                DailyBar(
                    date=bar.date,
                    open=bar.open,
                    high=max(bar.high, price),
                    low=min(bar.low, price),
                    close=price,
                    volume=quote.volume or bar.volume,
                ),
            ]
        return [
            *bars,
            DailyBar(
                date=today, open=price, high=price, low=price, close=price, volume=quote.volume
            ),
        ]

    def _quotes(self, symbols: list[str]) -> dict[str, Quote]:
        if not symbols:
            return {}
        try:
            return self.provider.get_quotes(symbols)
        except ProviderError:
            return {}

    def quote_symbols(self) -> list[str]:
        """Every symbol the next scan will want a quote for: the universe plus
        anything currently held, since an open position must be managed even
        after it leaves the watchlist.

        Split out so the caller can fetch quotes *before* opening the database.
        The provider call takes seconds and needs no database at all, and DuckDB
        allows a single writer — so fetching it on an open connection blocks every
        reader for the length of a network round trip. See `prefetched_quotes`.
        """
        universe = self.universe()
        try:
            account = self.account()
        except TrdError:
            return sorted({i.symbol for i in universe})
        held = self.positions.list_open(account.id)
        return sorted({i.symbol for i in universe} | {i.symbol for _, i in held})

    # ------------------------------------------------------------------ scan

    def scan(
        self,
        paper: bool = True,
        at: datetime | None = None,
        quotes: dict[str, Quote] | None = None,
    ) -> ScanResult:
        """One pass: manage exits first (freeing capacity), then take new entries.

        `quotes` lets the caller hand in prices fetched before the database was
        opened, which keeps a multi-second network round trip out of the window
        where this process holds DuckDB's single writer lock. Omitted, the scan
        fetches them itself and behaves exactly as before.
        """
        config = self.config()
        self._verify_rules(config)
        account = self.account()
        now = at or datetime.now()
        today = now.date()

        universe = self.universe()
        open_positions = self.positions.list_open(account.id)
        symbols = sorted({i.symbol for i in universe} | {i.symbol for _, i in open_positions})
        if quotes is None:
            quotes = self._quotes(symbols)
        run = self.runs.start(now, paper)

        result = ScanResult(
            run_id=run.id, at=now, paper=paper, scanned=len(universe), capacity=config.max_positions
        )

        closed_ids = self._run_exits(
            config, account, open_positions, quotes, today, now, paper, result
        )
        held = {
            instrument.id
            for position, instrument in open_positions
            if position.id not in closed_ids
        }
        self._run_entries(
            config, account, universe, quotes, today, now, paper, held, run.id, result
        )

        result.open_positions = len(self.positions.list_open(account.id))
        self.runs.finish(
            run.id,
            scanned=result.scanned,
            signals=len(result.signals),
            opened=len(result.opened),
            closed=len(result.closed),
        )
        return result

    def _run_exits(
        self,
        config: EngineConfig,
        account: Account,
        open_positions: list[tuple[EnginePosition, Instrument]],
        quotes: dict[str, Quote],
        today: date,
        now: datetime,
        paper: bool,
        result: ScanResult,
    ) -> set[int]:
        closed_ids: set[int] = set()
        for position, instrument in open_positions:
            stored = self._stored_bars(instrument.id)
            quote = quotes.get(instrument.symbol)
            bars = self._with_live_bar(stored, quote, today)
            if not bars:
                result.skipped.append(f"{instrument.symbol}: no price history for an open position")
                continue
            price = quote.price if quote is not None else bars[-1].close

            # Advance the state machine before the rules read it.
            live = position.model_copy(
                update={
                    "trail_high": max(position.trail_high, price),
                    "bars_held": sum(1 for b in stored if b.date > position.opened_at.date()),
                }
            )
            decision = evaluate_exits(live, bars, price, config.exit_params, now)
            if decision is None:
                self.positions.touch(position.id, live.trail_high, live.bars_held, bars[-1].date)
                continue

            pnl = live.pnl_at(price)
            fill = ScanFill(
                symbol=instrument.symbol,
                strategy=position.strategy,
                quantity=position.quantity,
                price=price,
                reason=decision.reason,
                rule=decision.rule,
                pnl=pnl,
                r_multiple=live.r_multiple_at(price),
            )
            if paper:
                self.txns.insert(
                    account_id=account.id,
                    instrument_id=instrument.id,
                    side=Side.SELL,
                    quantity=position.quantity,
                    price=price,
                    fees=Decimal(0),
                    executed_at=now,
                    note=f"engine {position.strategy} exit: {decision.rule}",
                )
                self.positions.touch(position.id, live.trail_high, live.bars_held, bars[-1].date)
                self.positions.close(position.id, now, price, decision.reason)
                closed_ids.add(position.id)
            result.closed.append(fill)
        return closed_ids

    def _run_entries(
        self,
        config: EngineConfig,
        account: Account,
        universe: list[Instrument],
        quotes: dict[str, Quote],
        today: date,
        now: datetime,
        paper: bool,
        held: set[int],
        run_id: int,
        result: ScanResult,
    ) -> None:
        # Without this a day-mode engine can enter at 15:50 and be flattened at
        # 15:55 by session_close — the spread paid twice for five minutes of
        # exposure. Entries stop before the flat time, exits keep running.
        cutoff = self._entry_cutoff(config.exit_params)
        if cutoff is not None and (now.hour * 60 + now.minute) >= cutoff:
            result.capacity = max(0, config.max_positions - len(held))
            result.skipped.append(
                f"no new entries within {ENTRY_CUTOFF_MINUTES}m of the session close — "
                "a fill now would be flattened before it could work"
            )
            return
        candidates: list[tuple[float, Instrument, list[DailyBar], int | None, ScanSignal]] = []
        for instrument in universe:
            stored = self._stored_bars(instrument.id)
            if not stored:
                result.skipped.append(
                    f"{instrument.symbol}: no price history — run 'trd sync --full'"
                )
                continue
            quote = quotes.get(instrument.symbol)
            if self._quote_is_stale(stored, quote, today):
                result.skipped.append(
                    f"{instrument.symbol}: no trade print yet today — the quote still reads "
                    f"{stored[-1].close}, the prior close, so a fill here would be fiction"
                )
                continue
            bars = self._with_live_bar(stored, quote, today)
            bar_date = bars[-1].date
            # Computed once per symbol, but applied *after* the strategy runs: a
            # signal blocked by earnings is a real signal on good data, unlike a
            # stale-quote one, and the passed-over signals are half the learning.
            blackout = self._earnings_blackout(instrument.id, today, config.earnings_blackout_days)
            withheld = False
            short_by: list[str] = []
            for key in config.strategies:
                strategy = STRATEGIES.get(key)
                if strategy is None:
                    continue
                if len(bars) < strategy.min_bars:
                    short_by.append(f"{key} needs {strategy.min_bars}")
                    continue
                signal = strategy.evaluate(bars)
                if signal is None:
                    continue

                price = bars[-1].close
                stored_signal = self.signals.get(instrument.id, key, bar_date)
                if stored_signal is None:
                    stored_signal = self.signals.insert(
                        run_id=run_id,
                        instrument_id=instrument.id,
                        strategy=key,
                        bar_date=bar_date,
                        fired_at=now,
                        price=price,
                        score=signal.score,
                        reason=signal.reason,
                    )
                    result.signals.append(
                        ScanSignal(
                            symbol=instrument.symbol,
                            strategy=key,
                            score=signal.score,
                            reason=signal.reason,
                            price=price,
                        )
                    )
                elif stored_signal.acted:
                    continue  # already traded on this bar's signal

                if blackout is not None:
                    withheld = True
                    continue  # fired, and logged, but earnings are too close to act on it
                if instrument.id in held:
                    continue  # already in the name; one position per symbol
                candidates.append(
                    (
                        signal.score,
                        instrument,
                        bars,
                        stored_signal.id,
                        ScanSignal(
                            symbol=instrument.symbol,
                            strategy=key,
                            score=signal.score,
                            reason=signal.reason,
                            price=price,
                        ),
                    )
                )
            if withheld and blackout is not None:
                days = (blackout - today).days
                when = "today" if days == 0 else f"in {days}d"
                result.skipped.append(
                    f"{instrument.symbol}: signal held back — earnings {when} ({blackout}). "
                    "A gap jumps the stop, so the trade would not risk the 1R it claims"
                )
            if short_by:
                result.skipped.append(
                    f"{instrument.symbol}: only {len(bars)} bars — {', '.join(short_by)} "
                    "(run 'trd sync --full')"
                )

        capacity = config.max_positions - len(held)
        result.capacity = max(0, capacity)
        if capacity <= 0 or not paper:
            return

        candidates.sort(key=lambda c: (-c[0], c[1].symbol))
        taken: set[int] = set()
        for _score, instrument, bars, signal_id, scan_signal in candidates:
            if capacity <= 0:
                break
            if instrument.id in taken or instrument.id in held:
                continue
            fill = self._open_position(
                config, account, instrument, bars, signal_id, scan_signal, now, result
            )
            if fill is None:
                continue
            result.opened.append(fill)
            taken.add(instrument.id)
            capacity -= 1
        result.capacity = max(0, capacity)

    def _open_position(
        self,
        config: EngineConfig,
        account: Account,
        instrument: Instrument,
        bars: list[DailyBar],
        signal_id: int | None,
        scan_signal: ScanSignal,
        now: datetime,
        result: ScanResult,
    ) -> ScanFill | None:
        price = bars[-1].close
        plan, skip = plan_entry(bars, config.position_size, config.exit_params)
        if plan is None:
            if skip is not None:
                result.skipped.append(f"{instrument.symbol}: {skip}")
            return None

        self.txns.insert(
            account_id=account.id,
            instrument_id=instrument.id,
            side=Side.BUY,
            quantity=plan.quantity,
            price=price,
            fees=Decimal(0),
            executed_at=now,
            note=f"engine {scan_signal.strategy} entry",
        )
        self.positions.open(
            account_id=account.id,
            instrument_id=instrument.id,
            signal_id=signal_id,
            strategy=scan_signal.strategy,
            opened_at=now,
            entry_price=price,
            quantity=plan.quantity,
            stop_price=plan.stop_price,
            target_price=plan.target_price,
            atr_at_entry=plan.atr,
            last_bar_date=bars[-1].date,
        )
        if signal_id is not None:
            self.signals.mark_acted(signal_id)
        for stored in result.signals:
            if stored.symbol == instrument.symbol and stored.strategy == scan_signal.strategy:
                stored.acted = True
        return ScanFill(
            symbol=instrument.symbol,
            strategy=scan_signal.strategy,
            quantity=plan.quantity,
            price=price,
            reason=scan_signal.reason,
        )

    # ----------------------------------------------------------------- views

    def signal_rows(self, limit: int = 25, strategy: str | None = None) -> list[SignalRow]:
        return [
            SignalRow(signal=signal, instrument=instrument)
            for signal, instrument in self.signals.list_recent(limit, strategy)
        ]

    def position_rows(
        self, open_only: bool = True, quotes: dict[str, Quote] | None = None
    ) -> list[PositionRow]:
        account = self.account()
        pairs = (
            self.positions.list_open(account.id)
            if open_only
            else self.positions.list_all(account.id)
        )
        if quotes is None:
            quotes = self._quotes(sorted({i.symbol for _, i in pairs}))
        rows: list[PositionRow] = []
        for position, instrument in pairs:
            quote = quotes.get(instrument.symbol)
            price = quote.price if quote is not None else self.prices.latest_close(instrument.id)
            rows.append(PositionRow(position=position, instrument=instrument, price=price))
        return rows

    def status(self, db_path: str = "") -> EngineStatus:
        """One object answering "what is this engine, and is it healthy?".

        No network call on purpose: this is the command you reach for when
        something looks wrong, and it must still answer when the provider is the
        thing that is wrong. Positions are marked at their last stored close, so
        the P&L is as fresh as the last sync rather than live.
        """
        config = self.config()
        account = self.account()
        universe = self.universe()
        open_pairs = self.positions.list_open(account.id)

        committed = sum((p.cost for p, _ in open_pairs), Decimal(0))
        unrealized = Decimal(0)
        stale = False
        for position, instrument in open_pairs:
            close = self.prices.latest_close(instrument.id)
            if close is None:
                stale = True
                continue
            unrealized += position.pnl_at(close) or Decimal(0)

        total, first, last_bar = self.prices.coverage()
        counts = self.prices.bar_counts()
        enabled = [STRATEGIES[k] for k in config.strategies if k in STRATEGIES]
        warmup = max((s.min_bars for s in enabled), default=0)
        short = sorted(
            (i.symbol, counts.get(i.id, 0)) for i in universe if counts.get(i.id, 0) < warmup
        )

        recent = self.runs.list_recent(1)
        midnight = datetime.combine(date.today(), datetime.min.time())
        return EngineStatus(
            build=build_version(),
            db_path=db_path,
            account=account.name,
            watchlist=config.watchlist,
            universe=[i.symbol for i in universe],
            strategies=list(config.strategies),
            position_size=config.position_size,
            max_positions=config.max_positions,
            earnings_blackout_days=config.earnings_blackout_days,
            flat_at_minute=int(config.exit_params.get("flat_at_minute", 0)),
            open_positions=len(open_pairs),
            committed=committed,
            unrealized=unrealized,
            marks_are_stale=stale,
            bars_total=total,
            bars_first=first,
            bars_last=last_bar,
            warmup_bars=warmup,
            short_history=short,
            last_scan=recent[0].started_at if recent else None,
            scans_today=self.runs.count_since(midnight),
        )

    def run_rows(self, limit: int = 25, today: bool = False) -> list[EngineRun]:
        """Scan history, newest first. The gaps are the point: a CronJob that
        stopped, or a scan that never fired, is invisible in every other view."""
        if today:
            midnight = datetime.combine(date.today(), datetime.min.time())
            return self.runs.list_since(midnight)
        return self.runs.list_recent(limit)

    def report(self) -> list[StrategyStat]:
        """Per-strategy scorecard over closed trades. The reason the dry run exists."""
        account = self.account()
        closed = [position for position, _ in self.positions.list_closed(account.id)]
        open_counts: dict[str, int] = {}
        for position, _ in self.positions.list_open(account.id):
            open_counts[position.strategy] = open_counts.get(position.strategy, 0) + 1
        return strategy_stats(closed, open_counts)


def scan_events(result: ScanResult) -> list[dict]:
    """One flat dict per thing that happened, ordered closes -> opens -> signals ->
    summary. Emitted as NDJSON for log shipping, so each event is independently
    queryable instead of buried in a rendered table.

    Money is floats here on purpose: this is telemetry headed for a dashboard, not
    the ledger. Every stored value stays Decimal.
    """

    def num(value: Decimal | None) -> float | None:
        return float(value) if value is not None else None

    stamp = result.at.isoformat()
    events: list[dict] = []
    for fill in result.closed:
        events.append(
            {
                "ev": "close",
                "ts": stamp,
                "symbol": fill.symbol,
                "strategy": fill.strategy,
                "rule": fill.rule,
                "quantity": num(fill.quantity),
                "price": num(fill.price),
                "pnl": num(fill.pnl),
                "r_multiple": num(fill.r_multiple),
                "reason": fill.reason,
            }
        )
    for fill in result.opened:
        events.append(
            {
                "ev": "open",
                "ts": stamp,
                "symbol": fill.symbol,
                "strategy": fill.strategy,
                "quantity": num(fill.quantity),
                "price": num(fill.price),
                "reason": fill.reason,
            }
        )
    for signal in result.signals:
        events.append(
            {
                "ev": "signal",
                "ts": stamp,
                "symbol": signal.symbol,
                "strategy": signal.strategy,
                "score": signal.score,
                "price": num(signal.price),
                "acted": signal.acted,
                "reason": signal.reason,
            }
        )
    events.append(
        {
            "ev": "scan",
            "ts": stamp,
            # Which code produced this. Groupable in Loki, so a stale rollout shows
            # up as a version that stopped changing instead of as absent behaviour.
            "version": build_version(),
            "run_id": result.run_id,
            "paper": result.paper,
            "scanned": result.scanned,
            "signals": len(result.signals),
            "opened": len(result.opened),
            "closed": len(result.closed),
            "open_positions": result.open_positions,
            "capacity": result.capacity,
            "skipped": len(result.skipped),
        }
    )
    return events


def _mean(values: Sequence[Decimal | None]) -> Decimal | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present, Decimal(0)) / Decimal(len(present))
