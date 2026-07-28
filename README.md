# trd — investment tracker

A local-first CLI for tracking real and paper investments: portfolio, lots, watchlists,
earnings, indicators, dollar-cost-averaging plans (with XIRR, forecasting, and backtests),
and a portfolio dashboard. Market data comes free from yfinance; your data lives in a
single DuckDB file. Full design: [DESIGN.md](DESIGN.md). Command reference: [CLAUDE.md](CLAUDE.md).

```
trd dashboard        # value, return, XIRR, vs S&P 500, today, top holding, movers
trd portfolio        # holdings, sorted by size, with weights + 30-day change
trd equity           # equity curve: value over time, return, XIRR, max drawdown
trd dca show -a sofi # a DCA plan's XIRR, per-symbol drift, cadence
trd sunday-prep      # week-ahead briefing: futures, macro events, earnings, levels, themes
trd engine report    # paper-trading engine: win rate, avg win/loss, expectancy per strategy
trd learn xirr       # the formula behind any number trd shows
```

## Sunday Prep — the week-ahead briefing

`trd sunday-prep` (alias `trd prep`) builds a structured, mentor-style briefing for the
trading week ahead: futures snapshot, the macro calendar (FOMC, jobs, CPI/PPI), earnings
from a curated large-cap universe, sector leadership, the VIX read, SPY/QQQ/IWM key
levels, themes, a study watchlist, and the week's risks. The narrative is deterministic —
no LLM, no network beyond yfinance — so it runs the same offline as on.

```bash
trd sunday-prep                 # render the briefing
trd sunday-prep --json          # structured JSON (for a Claude session to narrate)
trd sunday-prep --snapshot      # also write JSON + markdown to $TRD_HOME/prep/<date>.{json,md}
trd sunday-prep --date 2026-06-14   # build for a specific reference date
```

To have it run itself every Sunday evening on an always-on Mac (and sync the snapshot
to your other Macs via iCloud), see [deploy/README.md](deploy/README.md).

## The trading engine — paper-trading on rules

`trd engine` scans a small universe, fires entry signals, and manages every open trade
through five exit rules — all on a **simulation** account, so no real money is involved.
Its point is the scorecard: after running for a while, `trd engine report` tells you which
rules actually earned their risk.

```bash
trd engine init        # simulation account + 10-name universe + rule set
trd engine scan        # one pass: exits first, then the best-ranked new entries
trd engine positions   # open trades: entry, stop (↑ = trailing in force), target, R
trd engine signals     # every signal fired, taken or passed over, with its reason
trd engine report      # win rate, avg win/loss, expectancy in R, per strategy
trd engine rules       # what each entry strategy looks for, and all five exit rules
trd engine backtest    # replay the same rules over 10y of history — the report,
                       #   but with hundreds of trades instead of years of waiting
```

Four entry strategies (`momentum`, `breakout`, `pullback`, `macd_cross`), all gated on the
200-day trend filter. Exits run in a fixed order — `stop → trail → target → indicator →
time` — so capital protection always precedes profit-taking. Fills are ordinary
transactions, so `trd portfolio`, `trd equity` and XIRR work on the engine account
unchanged.

Read **expectancy** before win rate: 40% winners at +0.5R beats 70% winners that give it
back on the losers.

**Live vs replayed.** The scorecard needs ~144 trades per strategy before an edge is
distinguishable from luck, and live paper-trading produces ~39 a year. `trd engine
backtest` closes that gap: it replays the *same* rule code over stored daily bars
(run `trd sync --years 10` first) and prints the same scorecard, so live and
historical results sit on one scale. Gaps fill at the open, a bar that touches both
stop and target counts the stop, and a `--fill close` / `--no-blackout` rerun bounds
the assumptions. Treat every backtest number as an upper bound — today's universe is
the survivors, and simulated fills pay no spread.

**Running it unattended:**

| How | Where |
|---|---|
| k3s CronJob every 5 min, with a Telegram feed to your phone | [k3s/trd-engine/README.md](k3s/trd-engine/README.md) |
| launchd agent on a Mac, no cluster needed | [deploy/README.md](deploy/README.md) |

Run one or the other, never both — DuckDB allows a single writer.

## Requirements

- macOS or Linux, Python 3.13+
- [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

## Install

Clone the repo, then install `trd` as a global command (editable, so `git pull` takes
effect immediately — no reinstall unless dependencies change):

```bash
git clone https://github.com/silverbeer/trd.git ~/gitrepos/trd
cd ~/gitrepos/trd
uv tool install --editable .
```

This puts `trd` on your PATH (via `~/.local/bin`). If `trd: command not found`, ensure
`~/.local/bin` is on PATH (`uv tool update-shell`, then open a new terminal).

When a dependency changes (rare), refresh with:

```bash
uv tool install --editable ~/gitrepos/trd --reinstall
```

## Where your data lives — and the iCloud config

By default the database is at `~/.trd/trd.duckdb` (per machine, not shared). Override the
location with the `TRD_HOME` environment variable. **To share one database across Macs,
point `TRD_HOME` at an iCloud Drive folder.**

### Setup (do this on every Mac)

Add this line to `~/.zshrc`, then `source ~/.zshrc` (or open a new terminal):

```bash
export TRD_HOME="$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd"
```

Verify it took effect:

```bash
echo $TRD_HOME        # should print the iCloud path, not be empty
trd dashboard         # should show your real holdings
```

If `trd` shows "No open positions" or an empty database, `TRD_HOME` is unset in the current
shell — `source ~/.zshrc` or open a new terminal. (A stray empty `~/.trd/trd.duckdb` can be
deleted; your real data is in the iCloud folder.)

### The one rule: one machine at a time

DuckDB is **single-writer**. The whole `.duckdb` file syncs through iCloud, so:

- **Never run `trd` on both Macs at once.** Let iCloud finish syncing (the cloud icon in
  Finder clears) before switching machines.
- If two `trd` commands overlap, you'll see `Database is busy — another trd command is
  using it`. Harmless: wait a moment and retry.
- Running both simultaneously risks corrupting the file. If that happens, rebuild from a
  backup (below).

### Durable alternative: backup / restore

The `.duckdb` file is rebuildable — only your transactions, accounts, plans, watchlists,
and indicator config are irreplaceable (prices/earnings re-download via `trd sync`). Export
those to portable JSON and restore on another machine — no file-sync corruption risk:

```bash
trd backup ~/Downloads/trd-backup.json     # on the source Mac
# copy the JSON to the other Mac, then:
trd restore ~/Downloads/trd-backup.json    # rebuild the DB
trd sync                                   # re-download prices/earnings
```

This is also the cleanest way to **bootstrap a new Mac** without waiting for iCloud to
finish syncing a 20 MB+ binary.

## First run (fresh database)

```bash
trd init                      # create the database + default 'main' account
trd account add fidelity      # one account per brokerage
trd buy AAPL 10 --price 213.50 --account fidelity
trd sync --full               # download ~2 years of price history
trd portfolio
```

## Development

```bash
uv sync                                          # install dev deps
uv run pytest                                    # tests (no network — FakeProvider)
uv run ruff check . && uv run ruff format --check .
uv run ty check
```

Changes go through pull requests; CI (ruff + ty + pytest) must pass before merge.
