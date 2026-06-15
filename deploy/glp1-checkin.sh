#!/bin/zsh
# GLP-1 experiment check-in — LLY vs NVO paper sims, started 2026-06-14.
# Run by a dated launchd job on the mac mini (the single always-on trd writer).
# Captures the comparison DATA to an iCloud report file; open a Claude session and
# read it to get the leader-vs-laggard / momentum-vs-mean-reversion verdict.

set -eu

export TRD_HOME="${TRD_HOME:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd}"
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

# Log locally (launchd can't write iCloud without Full Disk Access).
LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/trd-glp1-checkin.log"

STAMP="$(date +%F)"
REPORT_DIR="$TRD_HOME/reports"
REPORT="$REPORT_DIR/glp1-checkin-$STAMP.md"

echo "=== $(date) :: glp1 check-in ===" >> "$LOG"
mkdir -p "$REPORT_DIR"
trd sync >> "$LOG" 2>&1 || echo "sync failed (continuing)" >> "$LOG"

{
  echo "# GLP-1 check-in — $STAMP"
  echo "_LLY vs NVO, \$100/month paper sims, started 2026-06-14._"
  echo
  echo '## LLY (leader)'
  echo '```'
  trd sim status --name lly
  echo '```'
  echo
  echo '## NVO (laggard)'
  echo '```'
  trd sim status --name nvo
  echo '```'
  echo
  echo '## All sims'
  echo '```'
  trd sim ls
  echo '```'
  echo
  echo '> Open a Claude session and ask for the verdict: which plan is ahead, each vs SPY,'
  echo '> and what it says about momentum (LLY) vs mean-reversion (NVO) for GLP-1.'
} > "$REPORT" 2>> "$LOG"

echo "=== wrote $REPORT ===" >> "$LOG"
