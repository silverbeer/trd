# Scheduled jobs (mac mini)

Two launchd agents live here: **Sunday Prep** (weekly briefing) and the
**trading engine** (every 5 minutes during market hours). Jump to
[Trading engine](#trading-engine-mac-mini) for the engine.

---

# Scheduled Sunday Prep (mac mini)

Run the week-ahead briefing automatically every Sunday evening and drop the snapshot
into iCloud, so every Mac — and a Claude Code session — reads a fresh briefing without
running anything live.

## What it does

`sunday-prep.sh` runs, in order:

1. `trd sync` — refresh quotes / daily bars / earnings.
2. `trd sunday-prep --snapshot` — build the briefing and write
   `$TRD_HOME/prep/<date>.json` (structured, for Claude/automation) and
   `<date>.md` (human-readable). Because `TRD_HOME` is an iCloud folder, the
   snapshot syncs to all your Macs.

## Install (on the mini)

```bash
# 1. trd on PATH (editable global install)
cd ~/gitrepos/trd && uv tool install --editable .

# 2. point the wrapper at your iCloud TRD_HOME (edit the file if needed)
#    deploy/sunday-prep.sh already defaults to the iCloud path.

# 3. edit the plist: replace USERNAME and the repo path with absolute paths
cp deploy/io.silverbeer.trd.sundayprep.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/io.silverbeer.trd.sundayprep.plist

# 4. dry-run it now (doesn't wait for Sunday)
launchctl start io.silverbeer.trd.sundayprep
cat "$TRD_HOME/prep/cron.log"
```

## The iCloud single-writer rule

DuckDB is single-writer and the whole file syncs through iCloud. The Sunday job
writes the DB, so **don't run `trd` on another Mac while the mini's job runs**
(Sunday ~6:05 PM). If two writers overlap you'll see `Database is busy`; harmless,
just retry once iCloud finishes syncing.

## Timezone

The original spec is "Sunday after 6:00 PM **Eastern**." launchd fires on the
machine's **local** time. Either set the mini to Eastern, or change `Hour` in the
plist so it lands after 6 PM ET in your timezone.

---

# Trading engine (mac mini)

> **There are two ways to run this.** The launchd agent below needs nothing but a
> Mac and is the fastest way to start collecting data. The k3s CronJob in
> [k3s/trd-engine](../k3s/trd-engine/README.md) adds a Telegram feed and a Grafana
> dashboard. **Run one, not both** — DuckDB allows a single writer, and two
> schedulers on one database will collide.

Run `trd engine scan` every 5 minutes during the regular session, so the engine
paper-trades a small universe unattended for a week and produces a per-strategy
scorecard at the end of it.

## What it does

`engine-scan.sh` runs, in order:

1. **Market-hours guard** — exits immediately unless it is Mon–Fri, 09:30–16:00
   **Eastern** (asked for explicitly, so the mini's own timezone doesn't matter).
2. **One `trd sync` per day** — pulls yesterday's settled bar. The scans in
   between use the live quote as today's forming bar, so one sync is enough.
3. **`trd engine scan`** — exits are evaluated before entries, so a rule that
   frees capital does it before new candidates compete for the slot.
4. **Publishes a snapshot** — writes `engine/status.txt` (open positions +
   scorecard) into the iCloud `trd` folder so any Mac can read the results.

Scanning the same bar repeatedly is safe by construction: a signal is stored once
per `(symbol, strategy, bar_date)` and stays a candidate only until it is acted
on, so the 5-minute cadence can never double-fill.

## Why the engine uses its own local database

`TRD_HOME` for everything else points at **iCloud**. DuckDB is single-writer and
the whole file syncs, so a job writing it every 5 minutes would fight `trd`
sessions on your other Macs and the Sunday Prep job — ~78 writes a day through
iCloud.

The engine only ever trades a *simulation* account, so it does not need the
portfolio DB. `engine-scan.sh` therefore defaults to a local
`TRD_HOME=$HOME/.trd-engine`, and publishes a few KB of **text** to iCloud
instead of a binary DB.

To override that and run against the shared iCloud DB, set `TRD_HOME` in the
wrapper — and expect the occasional `Database is busy`, which the script already
treats as "skip this pass" rather than a failure.

Engine state is not stranded on the mini: `trd backup` carries the engine config,
signals, and positions (stops, targets, trail high-water marks) along with the
transactions, and `trd restore` re-links them.

## Install (on the mini)

```bash
# 1. code + trd on PATH (editable global install, so git pull updates the binary)
cd ~/gitrepos/trd && git pull && uv tool install --editable .

# 2. create the engine's own database and universe
TRD_HOME=~/.trd-engine trd init
TRD_HOME=~/.trd-engine trd engine init
TRD_HOME=~/.trd-engine trd sync --full     # the rules need 200 bars of history

# 3. edit the plist: replace USERNAME with your account name
cp deploy/io.silverbeer.trd.engine.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/io.silverbeer.trd.engine.plist

# 4. verify the install without waiting for the opening bell
TRD_ENGINE_FORCE=1 zsh deploy/engine-scan.sh
tail -20 ~/Library/Logs/trd-engine-run.log
```

`TRD_ENGINE_FORCE=1` bypasses only the market-hours guard — everything else runs
for real.

## Reading the week

```bash
TRD_HOME=~/.trd-engine trd engine positions      # open trades, stop, target, R
TRD_HOME=~/.trd-engine trd engine signals        # every signal, taken or passed over
TRD_HOME=~/.trd-engine trd engine report         # the scorecard the dry run is for
TRD_HOME=~/.trd-engine trd portfolio -a engine-sim
```

From another Mac, read `$TRD_HOME/engine/status.txt` in iCloud instead.

Read **expectancy** before win rate: a 40% win rate at +0.5R per trade beats a
70% win rate that gives it all back on the losers.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/io.silverbeer.trd.engine.plist
rm ~/Library/LaunchAgents/io.silverbeer.trd.engine.plist
```
