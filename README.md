# trd — investment tracker

A local-first CLI for tracking real and paper investments: portfolio, lots, watchlists,
earnings, indicators, dollar-cost-averaging plans (with XIRR, forecasting, and backtests),
and a portfolio dashboard. Market data comes free from yfinance; your data lives in a
single DuckDB file. Full design: [DESIGN.md](DESIGN.md). Command reference: [CLAUDE.md](CLAUDE.md).

```
trd dashboard        # value, return, XIRR, vs S&P 500, today, top holding, movers
trd portfolio        # holdings, sorted by size, with weights + 30-day change
trd dca show -a sofi # a DCA plan's XIRR, per-symbol drift, cadence
trd learn xirr       # the formula behind any number trd shows
```

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
