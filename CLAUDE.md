# trd — Investment Tracker

Local-first investment tracker. Full architecture and roadmap: [DESIGN.md](DESIGN.md).

## Commands

```bash
uv sync                     # install deps
uv run trd --help           # CLI entry point
uv run pytest               # tests (no network — FakeProvider in tests/conftest.py)
uv run ruff check . && uv run ruff format --check .
uv run ty check             # type checking
```

## CLI quick reference

```bash
trd init                              # create ~/.trd/trd.duckdb + 'main' account
trd account add fidelity              # one account per brokerage (--type simulation for paper)
trd account ls
trd sync [--full]                     # refresh quotes + daily bars + earnings (--full = 2y backfill)
                                      # also pulls intraday bars for an intraday engine's universe,
                                      # driven by its config — no flag to forget
trd portfolio [--account NAME]        # holdings with live P&L
trd equity [--account NAME] [--days N | --months N] [--all] [--json]
                                      # equity curve: portfolio value over time, period return,
                                      # XIRR, max drawdown. Derived from txn FIFO × price_daily
                                      # (no snapshots); depth bounded by sync history
trd lots [SYMBOL] [--account NAME]    # per-purchase detail: buy date, paid/share, total cost, gain
trd history [--days 30] [--all-time] [--symbol X] [--side buy|sell] [--account N] [--all]
                                      # what you bought and sold, newest first, with realized P&L on
                                      # every sell (FIFO-matched) and a period total. Real money only
                                      # unless --all. FIFO matches over ALL history, then the window
                                      # filters what's shown — never the other way round
trd quote AAPL                        # live quote for any symbol
trd buy AAPL 10 [--price 213.50] [--account main] [--date 2026-06-10] [--fees 1] [--note ...]
trd sell AAPL 5 [--price ...]         # validates held quantity
trd import txns.csv                   # bulk-load transactions
trd backup data.json                 # export user-owned facts (txns/accounts/plans/watch/indicators)
trd restore data.json [--force]      # rebuild a DB from a backup, then trd sync (cross-machine sync)
trd watch add NVDA [--list ai]        # follow a symbol (creates list if needed)
trd watch rm NVDA [--list ai]
trd watch ls [ai]                     # quote board: price, day Δ%, 52w pos, vol/avg, next earnings
trd earnings [--days 14]              # upcoming earnings across everything tracked
trd exit set ABBV --account rh-agent --stop 221 [--target 244.81] [--note ...]
                                      # stop/target exit trigger on a holding (one per account+symbol)
trd exit ls|check|rm [--account NAME]  # ls = all triggers vs latest close; check = only breaches
                                      # rule fires on a daily CLOSE beyond the level (run trd sync)
trd indicators NVDA                   # indicator panel with plain-English readings
trd indicator ls|catalog|add|rm|info  # manage followed indicators (trd indicator add ema -p period=8)
trd dca set --account sofi --monthly 100 --day 15 --alloc SPY=40 --alloc QQQ=40 --alloc SMH=10 --alloc ARKX=10
                                      # DCA plan on ANY account; real accounts: you execute at
                                      # the broker, trd records + scores vs SPY ('trd plan' = alias)
trd dca invest [--account NAME] [--date 2026-01-15]    # record the month (once/month/plan)
trd dca show [--account NAME]         # flagship view: XIRR, per-symbol drift, cadence/streak
trd dca history [--limit N]           # every contribution event with legs and prices
trd dca forecast [--years 10] [--seed N]   # CAGR projection + Monte Carlo p10/p50/p90 bands
trd dca backtest [--years 10]         # replay the exact plan against real (adjusted) history
trd dca status|ls|edit|pause|resume   # quick view, list, partial update, lifecycle
                                      # edit --alloc IVV=25 --alloc IXUS=25 ... re-targets the plan
                                      # (weights sum to 100); recorded buys keep their symbols,
                                      # only future contributions follow the new split
trd sunday-prep [--json] [--snapshot] [--date ISO]   # alias 'trd prep'
                                      # week-ahead briefing: futures, macro calendar, curated-universe
                                      # earnings, sector leadership, VIX, SPY/QQQ/IWM levels, themes,
                                      # watchlist, risks. Deterministic narrative; --snapshot writes
                                      # TRD_HOME/prep/<date>.{json,md} (the scheduled mini job uses this)
trd learn [TERM]                      # investing dictionary: every term + exact formula trd uses
                                      # incl. engine terms (r-multiple, expectancy, survivorship);
                                      # indicator/strategy/exit entries generate from the registries
trd sync --years 10                   # deep backfill (forecast/backtest need long history)
trd sim init --monthly 100 [--strategy ticker|momentum] [--ticker SPY] [--alloc ...] [--name NAME]
                                      # sim = plan on a paper (simulation) account; sim invest/status same
trd engine init [--account NAME] [--size 1000] [--max 5] [--symbols A,B,...] [--strategies K,K]
                      [--sizing exposure|risk] [--timeframe 1d|5m|15m|30m|1h] [--flat-at 1555]
                      [--regime-sma 100] [--regime-vix-max 30]
                                                 # 'exposure' commits --size/trade (risk floats);
                                                 # 'risk' risks --size/trade (position size floats)
                                                 # --timeframe is the bar width the rules run on;
                                                 # a day engine (--flat-at) is refused on 1d bars,
                                                 # where a 2xATR stop can't be reached before the bell
                                                 # --regime-* gate NEW ENTRIES on the market, not the
                                                 # name: no buys while SPY is under its N-day or VIX
                                                 # is over V. Exits keep running. BOTH OFF BY DEFAULT
                                                 # ('trd engine status' shows the gate either way)
                                      # monitor-mode trading engine: paper-trades a 10-name universe
                                      # on a simulation account. Needs 'trd sync --full' (200 bars)
trd engine scan [--paper/--no-paper] [--json]   # one pass: exits first, then best-ranked entries
trd engine monitor [--interval 60] [--passes N] # live view on a terminal: book stays still, clock/
                                      # capacity/activity move. Piped or --ndjson falls back to scrolling
trd engine positions [--all]          # open trades: entry, stop (↑ = trailing in force), risk, target, R
                                      # risk = (mark − stop in force) × remaining qty, floored at 0 —
                                      # what this trade loses from here, not what it committed
trd engine trim SYM --pct 50          # sell part of an open position, leave the rest running.
                      [--quantity N] [--price P]   # for taking cash out without abandoning the
                                      # trade. Stop/target/trail untouched — trimming changes the
                                      # size, not the plan. Trimming ALL of it is refused: closing
                                      # goes through an exit rule so the trade records why it ended.
                                      # R stays honest — 90% at +2R then -1R on the rest = +1.7R,
                                      # measured against the size taken at entry.
                                      # NOTE 'trd sell' on an engine-held symbol is refused; it
                                      # would desync engine_position and take the account short
trd engine signals [-n 25] [-s KEY]   # every signal fired, taken or passed over, with its reason
trd engine report                     # per-strategy scorecard: win%, avg win/loss, expectancy in R
trd engine rules                      # what each entry strategy looks for + the 5 exit rules
trd engine why SYMBOL                 # why THIS trade was taken: the numbers the rule saw at entry,
                                      # what each indicator means, and which exit is in force
trd engine runs [-n N] [--today]      # scan history + interval between passes; ⚠ marks missing scans
trd engine status [--json]            # what this engine is + whether it's healthy: build, DB, rule set,
                                      # capacity, bar depth, last scan. No network — answers when yfinance doesn't
                                      # also: realized / unrealized / NET (all three, never the total alone)
                                      # and money at risk — what's lost if every stop hits, typically a
                                      # tenth of 'committed'. See 'trd learn risk-at-stop'
                                      # warns in red when the engine's own config is one 'init' would
                                      # refuse (a day engine on 1d bars). init can't catch that — the
                                      # engine already exists — and it reads as a flat strategy, not a
                                      # broken one. 'config_refused' in --json
trd engine backtest [--years N] [--fill intrabar|close] [--no-blackout] [--symbols A,B]
trd engine backtest --regime/--no-regime        # same history with the regime gate on and off —
                                      # the comparison the gate should be judged on, never assumed
                                      # replay the rules against stored history — same scorecard as
                                      # 'report', hundreds of trades per run. Needs 'trd sync
                                      # --years 10'. Day-mode engines backtest on their intraday
                                      # bars (the walk is keyed on each bar's instant, so
                                      # session_close fires at the bell); day mode on 1d is refused
trd engine scan --ndjson --notify     # one JSON event per line (log shipping) + Telegram on fills
trd engine reconcile broker.json [--account NAME]
                                      # diff a broker snapshot against what trd believes it holds:
                                      # per symbol ok / QUANTITY / MISSING AT BROKER / UNTRACKED,
                                      # plus how far trd's stored close sits from the broker's mark
                                      # (with its date — a gap there is stale bars, not bookkeeping).
                                      # Exits non-zero when they disagree. No network, no credentials:
                                      # the brokerage read is an authenticated MCP session that writes
                                      # the snapshot ([docs/robinhood-mcp.md](docs/robinhood-mcp.md))
```

## Deployment

Unattended runs live in two places — use one, never both (DuckDB is single-writer):

- **k3s CronJob** (the real one): [k3s/trd-engine/README.md](k3s/trd-engine/README.md).
  `./scripts/deploy-k3s.sh --test` seeds `~/.trd-engine`, builds + imports the image,
  applies the manifests with the hostPath rewritten to this machine, and runs one scan.
  Telegram setup (bot, chat id, verification) is documented there.
- **launchd agents** (no cluster): [deploy/README.md](deploy/README.md) — Sunday Prep,
  the engine scan, and `engine-publish.sh`, which copies the engine's `status.txt` and
  `engine-backup.json` into iCloud. The publisher pairs with *either* runner because it
  only copies files and never opens the database.

The engine's DB (`~/.trd-engine`) is deliberately separate from the real one and never in
iCloud: a k3s pod can't see a macOS FileProvider path, and iCloud resolves binary
conflicts by duplicating rather than merging.

## Output contract

Read commands take `--json`: the underlying model, full precision, stable keys, no colour,
no truncation, one document on stdout. Errors under `--json` are `{"error", "message"}` on
stdout with a non-zero exit, so one `| jq` handles success and failure alike. Spinners are
suppressed in JSON mode (they write to stdout and would corrupt the document). Rich tables
truncate to terminal width — never parse them.

CSV import format (header required): `date,account,symbol,side,quantity,price[,fees,note]` — date is ISO, side is buy/sell.

## Architecture rules (enforce in review)

- CLI layer ([src/trd/cli](src/trd/cli)) never touches DuckDB or yfinance directly — services only.
- Services ([src/trd/services](src/trd/services)) never import Typer/Rich — pure logic, fully testable.
- All market data goes through the `MarketDataProvider` protocol ([src/trd/providers/base.py](src/trd/providers/base.py)). Never import yfinance outside [src/trd/providers/yf.py](src/trd/providers/yf.py).
- Holdings are always derived from transactions via FIFO ([src/trd/services/fifo.py](src/trd/services/fifo.py)) — never stored as mutable balances.
- Schema changes = new numbered file in [src/trd/db/migrations](src/trd/db/migrations). Never edit an applied migration.
- Money/quantities are `Decimal` end to end. Never float.
- Broker integration is **agent-side only**: an MCP session reads the brokerage and writes a snapshot file; `trd engine reconcile` does the diff. Nothing under `src/trd` imports or knows about MCP. The committed `.claude/settings.json` (never `settings.local.json`, which is gitignored and would put the gate on one machine only) names all 53 tools the server exposes: 34 reads allowed, 19 denied — the 17 that mutate broker state (order place/cancel, option exercise, watchlist and scan mutations) plus both `review_*_order` tools, which price an order without placing it and are denied anyway because trd decides from its own data. There is no mid-name wildcard, so a tool added later matches neither list and surfaces as an unlisted tool needing an explicit decision. See [docs/robinhood-mcp.md](docs/robinhood-mcp.md).
- Static reference data (curated universe, FOMC/macro calendar) lives in [src/trd/data](src/trd/data) as plain Python — no YAML dep. `SundayPrepService` is pure (provider + data, no DuckDB); its briefing narrative is deterministic templates, leaving a seam for a future `--ai` pass.
- Engine rules are code-registry entries, never config: entry strategies in [src/trd/engine/strategies.py](src/trd/engine/strategies.py) (`@register`, mirroring the indicator registry), exit rules in [src/trd/engine/exits.py](src/trd/engine/exits.py). Strategies never reimplement indicator math — they call the indicator registry. Every signal and exit carries a plain-English `reason`; a rule you can't explain doesn't ship.
- The engine only ever trades a `simulation` account, and its fills are ordinary `txn` rows — so portfolio/equity/XIRR/drawdown work on it unchanged. `engine_position` stores only what a txn can't: strategy, stop/target, trail high-water mark, exit reason. The initial stop is immutable so closed-trade R-multiples stay meaningful.
- Tests never hit the network. Extend `FakeProvider` in [tests/conftest.py](tests/conftest.py).

## Environment

- DB lives at `~/.trd/trd.duckdb`; override root dir with `TRD_HOME` (tests do this).
- `trd` table for transactions is named `txn` (`transaction` is a reserved word).
