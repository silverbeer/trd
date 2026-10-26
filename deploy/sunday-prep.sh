#!/bin/zsh
# Sunday Prep cron wrapper — run by launchd on the mac mini Sunday evenings.
# Refreshes market data, writes the week-ahead briefing snapshot to
# $TRD_HOME/prep/<date>.{json,md} so every Mac (and a Claude session) can read
# it, and sends it as one Telegram message to the private alert chat (SB-1436).
#
# launchd does NOT load your shell profile, so set TRD_HOME and PATH explicitly
# below (or via the plist's EnvironmentVariables) before installing.

set -eu

# --- edit these for your machine --------------------------------------------
export TRD_HOME="${TRD_HOME:-$HOME/.trd-portfolio}"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# The runner's venv, refreshed by 'deploy/live-runner.sh install' on every
# deploy — the same binary alloc-report.sh uses. A 'uv tool install' copy on
# PATH is only as new as the last time someone remembered to reinstall it.
TRD="${TRD_BIN:-$HOME/.trd-runner/venv/bin/trd}"
# Telegram token and chat ids. Without it the briefing is still written; only
# the message is skipped.
ENV_FILE="${RH_ENV_FILE:-$HOME/.config/trd/live.env}"
# ----------------------------------------------------------------------------

# Keep the cron log on the LOCAL disk, not iCloud — a launchd agent can't write
# iCloud Drive until granted Full Disk Access, and we want a log of that very
# failure.
LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/trd-sundayprep-run.log"

notify=()
if [[ -r "$ENV_FILE" ]]; then
    set -a
    . "$ENV_FILE"
    set +a
    notify=(--notify)
else
    echo "no $ENV_FILE — briefing written, not sent" >> "$LOG"
fi

echo "=== $(date) :: sunday-prep run ===" >> "$LOG"
"$TRD" sync >> "$LOG" 2>&1 || echo "sync failed (continuing)" >> "$LOG"
"$TRD" sunday-prep --snapshot "${notify[@]}" >> "$LOG" 2>&1
echo "=== done ===" >> "$LOG"
