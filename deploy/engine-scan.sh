#!/bin/zsh
# Trading engine scan — run by launchd on the mac mini every 5 minutes.
# Safe to invoke as often as you like: it exits immediately outside regular
# trading hours, and a signal is stored once per (symbol, strategy, bar) so
# re-scanning the same bar all day can never double-fill.
#
# launchd does NOT load your shell profile, so set TRD_HOME and PATH explicitly.

set -eu

# --- edit these for your machine --------------------------------------------
# The engine runs against a LOCAL database, deliberately NOT the iCloud one.
#
# DuckDB is single-writer and the whole file syncs through iCloud. Scanning every
# 5 minutes would put ~78 writes/day through iCloud, colliding with `trd` sessions
# on your other Macs and with the Sunday Prep job. The engine only ever trades a
# simulation account, so it does not need to share the portfolio DB.
#
# To run it against the iCloud DB instead, point TRD_HOME at that folder — and
# expect "Database is busy" whenever another Mac is writing (handled below).
export TRD_HOME="${TRD_HOME:-$HOME/.trd-engine}"

# Where to publish a readable snapshot so other Macs can see results without
# opening the engine DB. Leave empty to skip publishing.
ICLOUD_TRD="${ICLOUD_TRD:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/trd}"

export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
# ----------------------------------------------------------------------------

LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/trd-engine-run.log"

# --- only run during the regular session -------------------------------------
# launchd fires on machine-local time; the market does not care what timezone the
# mini is set to, so ask for Eastern explicitly.
# TRD_ENGINE_FORCE=1 bypasses the window — use it to verify an install without
# waiting for the opening bell.
if [ "${TRD_ENGINE_FORCE:-0}" != "1" ]; then
    dow=$(TZ=America/New_York date +%u)          # 1=Mon ... 7=Sun
    now=$((10#$(TZ=America/New_York date +%H%M))) # 10# so 0930 isn't read as octal
    [ "$dow" -le 5 ] || exit 0
    { [ "$now" -ge 930 ] && [ "$now" -le 1600 ]; } || exit 0
fi

# Market holidays are not filtered. They are harmless: with no new daily bar the
# stored signals for the last bar already exist, so nothing new can fire.

echo "=== $(date) :: engine scan ===" >> "$LOG"

# --- refresh daily bars once per day ------------------------------------------
# The live quote forms today's bar, but yesterday's settled close still has to be
# pulled down. One sync per day is enough; the scans in between use the quote.
STAMP="$TRD_HOME/.last-sync"
today=$(TZ=America/New_York date +%F)
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$today" ]; then
    if trd sync >> "$LOG" 2>&1; then
        mkdir -p "$TRD_HOME"
        echo "$today" > "$STAMP"
    else
        echo "sync failed (continuing with stored bars)" >> "$LOG"
    fi
fi

# --- intraday bars, every pass ------------------------------------------------
# The daily stamp above is right for a daily engine and starves an intraday one.
# A 5-minute engine's bars settle every five minutes, and the live quote only
# forms the *current* bucket — every completed bar since the morning sync exists
# only if something wrote it. Left alone, by 15:00 the ATR sizing the stop is
# five hours stale. Cheap: engine universe only, fetched incrementally from the
# newest stored bar. A daily engine makes no provider call at all.
if ! trd sync --intraday-only >> "$LOG" 2>&1; then
    echo "intraday refresh failed (continuing with stored bars)" >> "$LOG"
fi

# --- earnings, every pass -----------------------------------------------------
# Bars settle once a day; earnings dates do not. yfinance publishes some of them
# mid-session, and the blackout can only protect a name whose date is already
# stored. Kept in step with deploy/engine-entrypoint.sh — two runners that drift
# apart is how one of them quietly stops protecting anything.
if ! trd sync --earnings-only >> "$LOG" 2>&1; then
    echo "earnings refresh failed (continuing with stored dates)" >> "$LOG"
fi

# --- the scan -----------------------------------------------------------------
# A non-zero exit here is nearly always "Database is busy" (another trd process
# holds the single writer lock). Skip this pass rather than failing the job —
# the next one is five minutes away and nothing is lost.
if ! trd engine scan >> "$LOG" 2>&1; then
    echo "scan skipped (db busy or provider error)" >> "$LOG"
    exit 0
fi

# --- publish a readable snapshot ----------------------------------------------
# A few KB of text, not a DuckDB file — safe to put in iCloud on every pass.
if [ -n "$ICLOUD_TRD" ]; then
    OUT="$ICLOUD_TRD/engine"
    if mkdir -p "$OUT" 2>>"$LOG"; then
        {
            echo "trd engine — last scan $(date)"
            echo
            trd engine positions 2>&1 || true
            echo
            trd engine report 2>&1 || true
        } > "$OUT/status.txt.tmp" && mv "$OUT/status.txt.tmp" "$OUT/status.txt"
    else
        # Usually means launchd lacks Full Disk Access for iCloud Drive.
        echo "snapshot skipped: cannot write $OUT" >> "$LOG"
    fi
fi

echo "=== done ===" >> "$LOG"
