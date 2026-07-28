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
trd portfolio [--account NAME]        # holdings with live P&L
trd equity [--account NAME] [--days N | --months N] [--all] [--json]
                                      # equity curve: portfolio value over time, period return,
                                      # XIRR, max drawdown. Derived from txn FIFO × price_daily
                                      # (no snapshots); depth bounded by sync history
trd lots [SYMBOL] [--account NAME]    # per-purchase detail: buy date, paid/share, total cost, gain
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
trd sync --years 10                   # deep backfill (forecast/backtest need long history)
trd sim init --monthly 100 [--strategy ticker|momentum] [--ticker SPY] [--alloc ...] [--name NAME]
                                      # sim = plan on a paper (simulation) account; sim invest/status same
trd engine init [--account NAME] [--size 1000] [--max 5] [--symbols A,B,...] [--strategies K,K]
                                      # monitor-mode trading engine: paper-trades a 10-name universe
                                      # on a simulation account. Needs 'trd sync --full' (200 bars)
trd engine scan [--paper/--no-paper] [--json]   # one pass: exits first, then best-ranked entries
trd engine monitor [--interval 60] [--passes N] # scan on a loop; Ctrl-C safe, every pass persisted
trd engine positions [--all]          # open trades: entry, stop (↑ = trailing in force), target, R
trd engine signals [-n 25] [-s KEY]   # every signal fired, taken or passed over, with its reason
trd engine report                     # per-strategy scorecard: win%, avg win/loss, expectancy in R
trd engine rules                      # what each entry strategy looks for + the 5 exit rules
trd engine backtest [--years N] [--fill intrabar|close] [--no-blackout] [--symbols A,B]
                                      # replay the rules against stored history — same scorecard as
                                      # 'report', hundreds of trades per run. Needs 'trd sync
                                      # --years 10'; day-mode (flat_at) configs are refused
trd engine scan --ndjson --notify     # one JSON event per line (log shipping) + Telegram on fills
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

CSV import format (header required): `date,account,symbol,side,quantity,price[,fees,note]` — date is ISO, side is buy/sell.

## Architecture rules (enforce in review)

- CLI layer ([src/trd/cli](src/trd/cli)) never touches DuckDB or yfinance directly — services only.
- Services ([src/trd/services](src/trd/services)) never import Typer/Rich — pure logic, fully testable.
- All market data goes through the `MarketDataProvider` protocol ([src/trd/providers/base.py](src/trd/providers/base.py)). Never import yfinance outside [src/trd/providers/yf.py](src/trd/providers/yf.py).
- Holdings are always derived from transactions via FIFO ([src/trd/services/fifo.py](src/trd/services/fifo.py)) — never stored as mutable balances.
- Schema changes = new numbered file in [src/trd/db/migrations](src/trd/db/migrations). Never edit an applied migration.
- Money/quantities are `Decimal` end to end. Never float.
- Static reference data (curated universe, FOMC/macro calendar) lives in [src/trd/data](src/trd/data) as plain Python — no YAML dep. `SundayPrepService` is pure (provider + data, no DuckDB); its briefing narrative is deterministic templates, leaving a seam for a future `--ai` pass.
- Engine rules are code-registry entries, never config: entry strategies in [src/trd/engine/strategies.py](src/trd/engine/strategies.py) (`@register`, mirroring the indicator registry), exit rules in [src/trd/engine/exits.py](src/trd/engine/exits.py). Strategies never reimplement indicator math — they call the indicator registry. Every signal and exit carries a plain-English `reason`; a rule you can't explain doesn't ship.
- The engine only ever trades a `simulation` account, and its fills are ordinary `txn` rows — so portfolio/equity/XIRR/drawdown work on it unchanged. `engine_position` stores only what a txn can't: strategy, stop/target, trail high-water mark, exit reason. The initial stop is immutable so closed-trade R-multiples stay meaningful.
- Tests never hit the network. Extend `FakeProvider` in [tests/conftest.py](tests/conftest.py).

## Environment

- DB lives at `~/.trd/trd.duckdb`; override root dir with `TRD_HOME` (tests do this).
- `trd` table for transactions is named `txn` (`transaction` is a reserved word).
