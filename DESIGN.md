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
- Caveat: single-writer. Fine for a CLI. If a background sync daemon ever appears, route all writes through one process.

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
│  SyncService, EarningsService,              │
│  IndicatorService, SimulationService        │
├──────────────────────┬──────────────────────┤
│  Repositories        │  MarketDataProvider  │
│  (DuckDB access)     │  (protocol)          │
│                      │  └─ YFinanceProvider │
├──────────────────────┴──────────────────────┤
│  DuckDB (~/.trd/trd.duckdb)                 │
└─────────────────────────────────────────────┘
```

Rules:

- CLI layer never touches the DB or yfinance directly.
- Services never import Typer/Rich — pure logic, fully testable.
- All external data validated through Pydantic models at the provider boundary.
- One DB connection manager; migrations as numbered SQL files applied at startup.

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
│   ├── cli/                  # Typer apps, one module per command group
│   │   ├── app.py            # root app, wires subcommands
│   │   ├── portfolio.py
│   │   ├── watch.py
│   │   ├── earnings.py
│   │   ├── sim.py
│   │   └── sync.py
│   ├── models/               # Pydantic domain models
│   ├── services/
│   ├── repos/                # DuckDB repositories
│   ├── providers/
│   │   ├── base.py           # MarketDataProvider protocol
│   │   └── yfinance.py
│   ├── db/
│   │   ├── connection.py
│   │   └── migrations/       # 001_init.sql, 002_...
│   └── indicators/           # pure functions: sma, ema, rsi, macd, ...
└── tests/
```

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
```

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

### Phase 1 — Portfolio core
Scaffold (uv, ruff, ty, pytest, CI-ready). Migrations, instrument/account/transaction tables. yfinance provider behind protocol. `trd init/sync/buy/sell/import/portfolio/quote`. Enter all existing real holdings. **Exit criteria: `trd portfolio` shows true positions with live-ish prices and P&L.**

### Phase 2 — Watchlists + earnings
Watchlist CRUD + quote board. Earnings calendar sync + `trd earnings`. Daily OHLCV backfill (2y) for all tracked instruments. **Exit: follow 50+ tickers, never surprised by an earnings date.**

### Phase 3 — Indicators + learning mode
Indicator code registry + `indicator_config` table + `trd indicator` management commands. `trd indicators <ticker>` panel with plain-English interpretations, indicator columns on watch board. Compute on the fly (no value cache yet). `trd portfolio history` charting.

### Phase 4 — Simulation account
Sim account type, `trd sim` commands, pluggable monthly strategy (start: fixed ticker or "strongest momentum on watchlist"), benchmark vs SPY.

### Phase 5 — Day-trading prep
Intraday data (yfinance 1m/5m bars), VWAP/gap/relative-volume, premarket scanner, trade journal (plan vs execution, R-multiple tracking). Gated on FINRA rule change for real money; works against sim account regardless.

### Phase 6 — AI agents
Trend-scan agent over watchlist, buy-candidate screener with rationale, scheduled morning brief. Built on Claude Agent SDK + the CLI as tool surface.

## Non-Goals (for now)

- No brokerage API integration (no auto-execution).
- No web UI.
- No real-time streaming data — sync-on-demand + scheduled syncs are enough until Phase 5.
- No tax-lot optimization (track FIFO lots, defer fancy accounting).

## Open Questions

- CSV import format: which brokerage(s)? Define mapping when first export is in hand.
- Intraday data retention policy (DuckDB will be fine for years of daily bars; minute bars need pruning).
- Sim strategy plug-in interface — decide after Phase 1 reveals service shapes.
