# trd — Investment Tracker

Design document. Local-first investment tracking, research, and (eventually) trading-assist tool.

## Vision

A personal CLI tool that grows in stages:

1. **Track** — know what I own (stocks + crypto), what it's worth, how it's doing.
2. **Watch** — follow many tickers across US exchanges, see earnings dates, key indicators.
3. **Learn** — surface the indicators that matter, build intuition for entries/exits.
4. **Simulate** — paper-trade $100/month into a simulation account, track hypothetical performance.
5. **Trade** — support a low-stakes day-trading workflow once FINRA's relaxed pattern-day-trader rules take effect (proposed drop of the $25k PDT minimum).
6. **Assist** — AI agents that scan watchlists, flag trends, and propose buy candidates.

Runs locally on two Macs (M5 mini + MacBook Air). No server, no cloud dependency for core function. Sync between machines via git (database is rebuildable from market data + a small set of user-owned facts).

## Tech Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Language | Python 3.13 | |
| Project mgmt | uv | `uv init`, `uv add`, lockfile committed |
| CLI | Typer + Rich | Rich tables/sparklines for terminal dashboards |
| Models | Pydantic v2 | Domain models + config + API response validation |
| Database | DuckDB | Single file `~/.trd/trd.duckdb`; great for analytical queries over price history |
| Market data | yfinance | Free, no key. Stocks, ETFs, crypto majors (BTC-USD, ETH-USD), earnings dates, fundamentals |
| Lint/format | ruff | |
| Types | ty | Astral type checker |
| Tests | pytest | + `pytest-cov`; fixtures with canned yfinance payloads, no network in tests |

### Why DuckDB

- Columnar, fast aggregations over years of daily OHLCV across hundreds of tickers.
- Single file, zero ops, perfect for local-first.
- SQL window functions make indicator computation (moving averages, RSI components) easy in-database.
- Native `DECIMAL(24, 8)`, which is what makes "money is `Decimal` end to end, never float" enforceable at the storage boundary rather than by convention.
- Caveat: single-writer. This stopped being theoretical once the engine started scanning every five minutes from a k3s CronJob — a writer excludes all readers, so the CLI became unusable against a live engine database during market hours.

  The lesson turned out not to be about DuckDB. Measured, a scan held the lock for 6.27s, of which **6.23s (99.5%) was the yfinance network call doing no database work at all** — the connection was opened at process start and held across the round trip. Fetching quotes before opening the connection dropped the window to 0.04s. Holding an exclusive lock across network I/O would hurt in any single-writer store, and SQLite's WAL would have masked it while costing the decimal storage above.

  Standing rule: **never hold a connection across a network call.** Read what you need, close, fetch, reopen to write. `connect()` also retries a held lock with bounded backoff, so a collision is a pause rather than an error.

### Why yfinance (and its risk)

- No API key, no hard rate limit, widest free coverage: quotes, history, earnings calendar, fundamentals, crypto majors.
- **Risk**: unofficial Yahoo scraper; breaks occasionally when Yahoo changes endpoints.
- **Mitigation**: all market data goes through a `MarketDataProvider` protocol (interface). yfinance is the first implementation. If it breaks or we outgrow it, swap in Finnhub/Alpha Vantage/CoinGecko behind the same interface without touching business logic.

## Architecture

```
┌─────────────────────────────────────────────┐
│  CLI (Typer)          trd <command>         │
│  thin: parse args, call service, render Rich │
├─────────────────────────────────────────────┤
│  Services (business logic)                  │
│  PortfolioService, WatchlistService,        │
│  SyncService, EarningsService, PlanService, │
│  IndicatorService, SundayPrepService,       │
│  EngineService, BacktestService             │
├──────────────────────┬──────────────────────┤
│  Repositories        │  MarketDataProvider  │
│  (DuckDB access)     │  (protocol)          │
│                      │  └─ YFinanceProvider │
├──────────────────────┴──────────────────────┤
│  DuckDB (~/.trd/trd.duckdb, and one file   │
│  per engine: ~/.trd-engine, ~/.trd-day)    │
└─────────────────────────────────────────────┘
```

Rules:

- CLI layer never touches the DB or yfinance directly.
- Services never import Typer/Rich — pure logic, fully testable.
- All external data validated through Pydantic models at the provider boundary.
- One DB connection manager; migrations as numbered SQL files applied at startup. Never edit an applied migration.
- Money and quantities are `Decimal` end to end. Never float.
- Holdings are always derived from transactions via FIFO — never stored as mutable balances.
- Never hold a database connection across a network call (see Why DuckDB).
- Tests never hit the network; extend `FakeProvider` in `tests/conftest.py`.

### Package layout

```
trd/
├── pyproject.toml
├── DESIGN.md
├── CLAUDE.md                  # project instructions for Claude Code
├── .claude/
│   └── skills/               # Claude tasks that drive the CLI (see below)
├── src/trd/
│   ├── __init__.py
│   ├── cli/
│   │   ├── app.py            # every Typer command, one root app
│   │   └── render.py         # every Rich renderable — no service imports Rich
│   ├── models/               # Pydantic domain models
│   ├── services/             # business logic, never imports Typer/Rich
│   ├── repos/                # DuckDB repositories
│   ├── providers/
│   │   ├── base.py           # MarketDataProvider protocol
│   │   └── yf.py
│   ├── db/
│   │   ├── connection.py
│   │   └── migrations/       # 001_init.sql, 002_...
│   ├── indicators/           # code registry: sma, ema, rsi, macd, atr, ...
│   ├── engine/               # code registry: entry strategies + exit rules
│   ├── learn/                # the `trd learn` glossary
│   ├── notify/               # Telegram push for engine fills
│   ├── data/                 # static reference data (curated universe, macro calendar)
│   └── build.py              # which commit this build is, for provenance
├── k3s/                      # CronJob manifests for the engine
├── deploy/                   # launchd agents + container entrypoint
└── tests/
```

The CLI is one `app.py` rather than a module per command group: Typer wires subcommands from a single tree, and splitting it bought indirection without removing anything. `render.py` is the hard boundary — services never import Rich, so every service is testable without a terminal.

## Data Model

Core entities (DuckDB tables, mirrored by Pydantic models):

- **instrument** — ticker, name, type (`stock | etf | crypto`), exchange, sector. One row per thing trackable.
- **account** — name, type (`real | simulation`), currency. Real holdings and the $100/month sim account are the same machinery, different account type.
- **transaction** — account, instrument, side (`buy | sell`), quantity, price, fees, timestamp, note. Holdings are *derived* from transactions (never stored as mutable balances) — gives full audit trail and accurate cost basis (FIFO lots).
- **price_daily** — instrument, date, OHLCV. Backfilled + synced from provider.
- **quote_snapshot** — intraday quotes captured during syncs (lightweight, prunable).
- **watchlist** / **watchlist_item** — named lists ("AI plays", "dividend", "crypto"), many-to-many to instruments.
- **earnings_event** — instrument, date, time-of-day (BMO/AMC), EPS estimate, actual (filled after report).
- **indicator_config** — the user's evolving list of followed indicators (see Indicator Data Model below).
- **indicator_value** *(later phase)* — optional cache of computed indicator values (see below).
- **plan** / **plan_leg** — DCA plans: cadence, monthly amount, per-symbol allocation. Contributions are recorded as ordinary transactions.
- **exit_trigger** — a stop/target level watched on a holding; fires on a daily close beyond the level.
- **engine_config** / **engine_run** / **engine_signal** / **engine_position** — the trading engine (below).
- **prep_snapshot** — archived Sunday Prep briefings.

Derived views (SQL views, not tables): current holdings per account, cost basis, unrealized P&L, portfolio value time series.

## CLI Surface (target shape)

```
trd init                          # create db, run migrations
trd sync [--full]                 # refresh quotes + daily bars + earnings for all tracked instruments

trd portfolio                     # holdings table: qty, cost basis, value, day Δ, total P&L
trd portfolio history [--period]  # value-over-time chart (Rich sparkline/plotext)
trd buy AAPL 10 --price 213.50 [--account main] [--date ...]
trd sell AAPL 5  --price 220.00
trd import <csv>                  # bulk-load existing positions/transactions

trd watch add NVDA [--list ai]
trd watch rm NVDA
trd watch ls [list]               # quote board: price, day Δ%, 52w range position, vol vs avg
trd quote AAPL                    # deep single-ticker view: price, key stats, indicators, next earnings

trd earnings [--days 14]          # upcoming earnings across portfolio + watchlists

trd indicators AAPL               # indicator panel with plain-English read (learning mode)
trd indicator ls|catalog|add|rm|info   # manage the followed-indicator list (see Indicator Data Model)

trd sim init --monthly 100        # create simulation account
trd sim invest                    # execute this month's $100 buy (strategy-driven)
trd sim status                    # sim performance vs benchmark (SPY)

trd dca set|invest|show|forecast|backtest   # recurring contributions on any account
trd exit set|ls|check              # stop/target triggers on a holding
trd prep [--snapshot]              # week-ahead briefing (futures, macro, levels, themes)
trd learn [TERM]                   # the dictionary: every term + the exact formula trd uses

trd engine init|scan|monitor       # the trading engine (see below)
trd engine status|runs|positions|report|signals|rules
trd engine backtest [--years 10]   # replay the same rules against history
```

Every read command takes `--json`: the underlying model, full precision, stable
keys, errors as JSON with a non-zero exit. Rich tables truncate to terminal
width — never parse them.

## The trading engine

The largest thing built since this document was first written, and the reason
Phase 5 arrived earlier than planned. `trd engine` scans a small universe on a
schedule, fires entry signals, and manages every open trade through exit rules —
all on a **simulation** account, so no real money is involved.

### Rules are code, never config

Entry strategies live in `engine/strategies.py` and exit rules in
`engine/exits.py`, both as `@register`-style code registries mirroring the
indicator one. Strategies never reimplement indicator math — they call the
indicator registry. Every signal and exit carries a plain-English `reason`: a
rule you cannot explain does not ship.

Exits run in a fixed order, capital protection before profit-taking:

```
stop → trail → target → indicator → time → session_close
```

The initial stop never moves. 1R is measured from it, so a drifting stop would
make every closed trade's R-multiple mean something different.

### Fills are ordinary transactions

The engine only ever trades a `simulation` account, and its fills are plain `txn`
rows — so portfolio, equity, XIRR and drawdown work on the engine account without
a line of new code. `engine_position` stores only what a transaction cannot
express: which rule fired, the stop and target, the trailing high-water mark, and
the exit reason.

### Two engines, two databases

A **swing** engine (`~/.trd-engine`) carries positions overnight. A **day** engine
(`~/.trd-day`) sets `flat_at_minute` and is flat by the bell, refusing new entries
in the last 30 minutes. Both run as k3s CronJobs every 5 minutes during market
hours, against separate databases — deliberately *not* the real one, and never in
iCloud, since a pod cannot see a macOS FileProvider path and iCloud resolves
binary conflicts by duplicating rather than merging.

### Backtesting: the statistical-power problem

The engine could explain every rule but not justify any of them. For a 2R-target
system the per-trade spread is ~1.2R, so telling a 0.2R edge from noise needs
~144 trades **per strategy** — about four years of live paper trading. `trd engine
backtest` replays the same rule code over stored daily bars and produces that
sample in about a minute.

It is a *driver* around the live rules, never a second copy: entries from the
strategy registry, exits from `evaluate_exits`, sizing from the same `plan_entry`
the live engine uses. What daily bars cannot express is decided explicitly rather
than silently — gaps fill at the open, a bar touching both stop and target counts
the stop, and day-mode configs are refused outright because daily bars have no
clock. Lookahead is guarded structurally (a strategy sees only `bars[:i+1]`) and
tested by rewriting the future and asserting past decisions are unchanged.

Results are an upper bound, not a forecast: survivorship, no slippage or spread,
and retroactively adjusted prices. The output says so every time.

### Operational lessons worth keeping

- **A deployed engine must state its provenance.** A stale image once ran rules
  from before `session_close` existed for a full session — the day engine held
  overnight, every test passed, and nothing anywhere said which code was running.
  The git SHA is now baked at image build and surfaces in `trd engine status`,
  `status.txt` and every scan event. A config that switches on a parameter whose
  rule is missing from the build now refuses to trade.
- **Silence and failure must be distinguishable.** `trd engine runs` shows the
  interval between scans, because "the engine did nothing" and "the engine never
  ran" look identical without the cadence.

## Indicator Data Model (evolvable by design)

The set of indicators I follow will change as I learn. The model splits three layers so adding/removing an indicator is a data change, not a schema or code change.

### Layer 1 — Code registry (Python)

Each indicator is a pure class registered by key. The math lives here; the library can hold 30 indicators while only 8 are followed.

```python
@register("rsi")
class RSI(Indicator):
    key = "rsi"
    category = Category.MOMENTUM
    default_params = {"period": 14}
    components = ["value"]              # what compute() returns

    def compute(self, bars: DataFrame, period: int) -> DataFrame: ...
    def interpret(self, latest: dict) -> str:
        """Plain-English read: '>70 overbought, <30 oversold' etc."""
```

Adding a brand-new indicator = write one class. Nothing else changes.

### Layer 2 — `indicator_config` table (the evolving list)

```sql
CREATE TABLE indicator_config (
    id            INTEGER PRIMARY KEY,
    key           TEXT NOT NULL,        -- matches code registry: 'rsi', 'sma'
    params        JSON NOT NULL,        -- {"period": 14} — overrides defaults
    enabled       BOOLEAN DEFAULT true,
    display_order INTEGER,
    note          TEXT,                 -- learning journal: why added, what it tells me
    added_at      TIMESTAMP,
    disabled_at   TIMESTAMP             -- soft remove — keep history of what was tried
);
```

- Add/remove an indicator = row change, zero code.
- Same `key` twice with different params is valid (`sma` 50 and `sma` 200 = two rows).
- `note` is a learning log: why followed, what it taught me.
- Soft-disable, never delete — preserves the record of what was tried and dropped.
- Startup validation: config row whose `key` is missing from the code registry gets warned about and auto-disabled. Config can never break the app.

### Layer 3 — `indicator_value` cache (optional, later)

```sql
CREATE TABLE indicator_value (
    instrument_id  INTEGER,
    indicator_key  TEXT,
    params_hash    TEXT,      -- hash of params JSON — rsi(14) ≠ rsi(21)
    date           DATE,
    components     JSON       -- {"value": 63.2} or {"macd": .., "signal": .., "hist": ..}
);
```

- `components` as JSON: MACD returns 3 series, Bollinger 3 bands, RSI 1 value — one shape fits all, no migration per new indicator.
- Cache only, never source of truth: everything is derivable from `price_daily`. Changed params produce a different `params_hash`, so recomputes never collide with stale rows.
- Phase 3 ships **without** this table — compute on the fly (DuckDB window functions handle years of daily bars instantly). Add the cache only if the watch board gets slow at 100+ tickers.

### Indicator CLI

```
trd indicator ls                  # followed list + categories + notes
trd indicator catalog             # everything available in the code registry
trd indicator add rsi --param period=14 --note "watching for divergence"
trd indicator rm macd             # soft-disable, keeps note + history
trd indicator info rsi            # full description + interpretation guide
```

`trd indicators <ticker>` renders its panel from enabled `indicator_config` rows in `display_order`, each with its `interpret()` one-liner.

## Key Indicators (learning focus)

Build these in `indicators/` as pure functions over price history; expose via `trd indicators <ticker>` with a one-line plain-English interpretation each. Start set (these seed `indicator_config` on `trd init`):

**Trend**
- SMA/EMA 20/50/200 — price above/below, golden/death cross
- MACD — momentum shifts

**Momentum**
- RSI(14) — overbought >70 / oversold <30
- 52-week range position

**Volume**
- Volume vs 20-day average — confirms moves

**Volatility**
- ATR — position sizing input (matters for day trading)
- Bollinger Bands

**Fundamentals (from yfinance)**
- P/E, forward P/E, PEG, market cap, short interest, beta
- Earnings date proximity — volatility event warning

Day-trading phase adds: VWAP, premarket gap %, relative volume. Designed but not built until Phase 5.

## Claude Tasks Integration

Project `CLAUDE.md` documents the CLI so Claude sessions can drive it. Skills under `.claude/skills/`:

- **morning-brief** — run `trd sync`, summarize portfolio moves, flag watchlist items with unusual volume or earnings this week.
- **earnings-week** — what reports in the next 7 days, with positions/exposure.
- **research <ticker>** — pull `trd quote`/`trd indicators` output + web research, produce a structured read.
- **sim-month** — run the monthly simulation buy, log the rationale.

Later (Phase 6) these evolve into scheduled agents (cron via Claude scheduled tasks) that run the brief every market morning.

## Phased Roadmap

Status as of July 2026: Phases 1–4 are **done**. Phase 5 was overtaken by the
trading engine, which delivered the R-multiple journal and rule-driven execution
it called for, on daily bars and a simulation account rather than intraday data.
Phase 6 has not started.

### Phase 1 — Portfolio core
Scaffold (uv, ruff, ty, pytest, CI-ready). Migrations, instrument/account/transaction tables. yfinance provider behind protocol. `trd init/sync/buy/sell/import/portfolio/quote`. Enter all existing real holdings. **Exit criteria: `trd portfolio` shows true positions with live-ish prices and P&L.**

### Phase 2 — Watchlists + earnings
Watchlist CRUD + quote board. Earnings calendar sync + `trd earnings`. Daily OHLCV backfill (2y) for all tracked instruments. **Exit: follow 50+ tickers, never surprised by an earnings date.**

### Phase 3 — Indicators + learning mode
Indicator code registry + `indicator_config` table + `trd indicator` management commands. `trd indicators <ticker>` panel with plain-English interpretations, indicator columns on watch board. Compute on the fly (no value cache yet). `trd portfolio history` charting.

### Phase 4 — Simulation account
Sim account type, `trd sim` commands, pluggable monthly strategy (start: fixed ticker or "strongest momentum on watchlist"), benchmark vs SPY.

### Phase 5 — Day-trading prep *(superseded by the trading engine)*
Originally: intraday data (yfinance 1m/5m bars), VWAP/gap/relative-volume, premarket scanner, trade journal with R-multiple tracking.

What shipped instead: the engine above, plus Sunday Prep (`trd prep`) for the week-ahead briefing. R-multiple tracking, rule-driven entries and exits, and the plan-vs-execution journal all exist.

Intraday data landed later, once the day engine's daily-bar version proved inert: a stop at 2 x the *daily* ATR cannot be reached inside one session, so every trade exited on the clock and the R-multiples described a risk profile the engine never ran. Measured on a live universe, a 2 x ATR stop sat 6–22% away on daily bars and 0.5–1.3% away on 5-minute bars. `price_intraday`, `MarketDataProvider.get_intraday_bars`, and `engine_config.timeframe` now let the rules run on 5m/15m/30m/1h bars, and the backtest walks bar instants so `session_close` has a real clock. A day-mode config on daily bars is refused outright.

**FINRA's PDT rule is no longer a gate.** The SEC approved amendments to Rule 4210 on 2026-04-14; effective **2026-06-04** both the $25,000 minimum equity requirement and the "pattern day trader" designation were eliminated, replaced by a $2,000 standard Reg T minimum plus risk-based intraday margin. Firms have an 18-month phase-in ending 2027-10-20, so broker behaviour still varies. What gates a live day engine now is evidence, not regulation: the day strategies backtest at -0.05R / +0.02R / -0.05R / -0.04R over 619 trades.

### Phase 6 — AI agents *(not started)*
Trend-scan agent over watchlist, buy-candidate screener with rationale, scheduled morning brief. Built on Claude Agent SDK + the CLI as tool surface.

The groundwork is the CLI itself: every read command emits `--json` — the underlying model, full precision, stable keys, errors as JSON with a non-zero exit — so an agent parses a contract rather than scraping Rich tables, which truncate to terminal width and say nothing about it.

## Non-Goals (for now)

- No auto-execution **yet** — and this is now a choice, not a limitation. The non-goal was written when the only route to a broker was a reverse-engineered API. Robinhood shipped an official agentic-trading MCP server in June 2026 (`https://agent.robinhood.com/mcp/trading`) that reads positions, balances and orders and places real equity and options trades into a dedicated, separately funded account. The route exists; what is missing is evidence that the rules deserve real money. The engine paper-trades and records; a human executes.

  When that changes, the shape is settled: **trd keeps deciding, the broker only executes.** The MCP product is built for a language model to make the calls, and using it that way would trade a measured, backtested, explainable edge for an unmeasurable one — the same reason `a rule you can't explain doesn't ship`. Execution belongs behind its own protocol alongside `MarketDataProvider`, fills get *read* rather than assumed (today `_open_position` writes a txn at the bar's close and trusts it; real orders partial-fill, get rejected, and slip), and the "engine only ever trades a simulation account" invariant needs an equally strong replacement rather than deletion. First milestone is reconciliation only: read real positions, compare against what the engine believes, place nothing. That gap is the number no backtest shows.
- No web UI.
- No real-time streaming data — scheduled syncs plus a live quote folded into the forming bar have been enough, including for the day engine.
- No tax-lot optimization (track FIFO lots, defer fancy accounting).

## Open Questions

Resolved:

- ~~CSV import format~~ — `date,account,symbol,side,quantity,price[,fees,note]`, defined once a real export was in hand.
- ~~Sim strategy plug-in interface~~ — answered twice over: DCA plans for scheduled contributions, and the engine's code registries for rule-driven trading. Both landed as registries rather than plug-ins, so a new rule is one class and no configuration.

Open:

- Intraday data retention, now that intraday bars *are* stored. Daily is comfortable — ~99k rows across 25 symbols and a decade. Intraday is bounded for free at present because the provider only serves ~60 days of 5-minute history, so the table self-limits; a longer-horizon source would need a pruning policy. `price_intraday` is deliberately a separate table from `price_daily` so a decade of daily reads never pays for it.
- Whether the engine should trim strategies automatically. The backtest grades them (breakout +0.29R over 286 trades; pullback +0.03R over 451), and the drift panel shows when an edge expires, but acting on that stays a human decision. Deliberately: the tool builds confidence in a choice, it does not make the choice.
- How to keep two machines' engine databases coherent. `trd backup` / `trd restore` handles the real database, but the engine databases are excluded from iCloud on purpose and currently live on one machine.
