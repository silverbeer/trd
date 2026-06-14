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
