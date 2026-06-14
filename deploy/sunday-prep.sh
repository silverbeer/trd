#!/bin/zsh
# Sunday Prep cron wrapper — run by launchd on the mac mini Sunday evenings.
# Refreshes market data, then writes the week-ahead briefing snapshot to
# $TRD_HOME/prep/<date>.{json,md} so every Mac (and a Claude session) can read it.
#
# launchd does NOT load your shell profile, so set TRD_HOME and PATH explicitly
# below (or via the plist's EnvironmentVariables) before installing.

set -eu

# --- edit these for your machine --------------------------------------------
export TRD_HOME="${TRD_HOME:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd}"
# uv tool install puts the trd binary on ~/.local/bin
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# ----------------------------------------------------------------------------

# Keep the cron log on the LOCAL disk, not iCloud — a launchd agent can't write
# iCloud Drive until granted Full Disk Access, and we want a log of that very
# failure. (trd's snapshot still writes to iCloud and needs the FDA grant.)
LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/trd-sundayprep-run.log"

echo "=== $(date) :: sunday-prep run ===" >> "$LOG"
trd sync                 >> "$LOG" 2>&1 || echo "sync failed (continuing)" >> "$LOG"
trd sunday-prep --snapshot >> "$LOG" 2>&1
echo "=== done ===" >> "$LOG"
