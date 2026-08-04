#!/bin/sh
# Container entrypoint for the trd engine CronJob.
#
# Same shape as deploy/engine-scan.sh (the launchd wrapper), minus the iCloud
# snapshot: in k3s the scan output IS the artifact, captured by `kubectl logs`.
#
# Any argument turns this into a passthrough, so the image doubles as a debug
# shell for the rest of the CLI:
#
#   kubectl exec -n trd deploy/... -- trd engine report
#   kubectl run -n trd trd-shell --image=trd:latest --rm -it -- engine positions

set -eu

# Passthrough mode: run whatever trd subcommand was asked for and stop.
if [ $# -gt 0 ]; then
    exec trd "$@"
fi

# --- only scan during the regular session -------------------------------------
# The CronJob schedule already narrows this to 09:00-16:55 ET Mon-Fri; the guard
# trims the edges cron cannot express (the 09:30 open and the 16:00 close).
# TRD_ENGINE_FORCE=1 bypasses it, for verifying a deploy off-hours.
if [ "${TRD_ENGINE_FORCE:-0}" != "1" ]; then
    # %-H / %-M are unpadded on purpose: POSIX arithmetic reads a leading zero as
    # octal, and "0930" is not a valid octal number. This is /bin/sh (dash), so
    # bash's 10# prefix is not available either.
    dow=$(TZ=America/New_York date +%u)           # 1=Mon ... 7=Sun
    hour=$(TZ=America/New_York date +%-H)
    minute=$(TZ=America/New_York date +%-M)
    now=$((hour * 100 + minute))
    if [ "$dow" -gt 5 ]; then
        echo "market closed (weekend) — nothing to do"
        exit 0
    fi
    if [ "$now" -lt 930 ] || [ "$now" -gt 1600 ]; then
        echo "outside 09:30-16:00 ET (now $now) — nothing to do"
        exit 0
    fi
fi

# Market holidays are not filtered. They are harmless: with no new daily bar,
# signals for the last bar already exist, so nothing new can fire.

# --- refresh daily bars once per day ------------------------------------------
# The live quote forms today's bar; yesterday's settled close still has to be
# pulled down. One sync a day is enough — the scans between use the quote.
STAMP="${TRD_HOME}/.last-sync"
today=$(TZ=America/New_York date +%F)
if [ "$(cat "$STAMP" 2>/dev/null || true)" != "$today" ]; then
    if trd sync; then
        echo "$today" > "$STAMP"
    else
        echo "sync failed — continuing with stored bars"
    fi
fi

# --- refresh intraday bars every pass -----------------------------------------
# The daily-stamp above is right for a daily engine and starves an intraday one.
# A 5-minute engine's bars settle every five minutes, and the live quote only
# ever forms the *current* bucket — every completed bar since the morning sync
# exists only if something wrote it. Left alone, by 15:00 the ATR sizing the stop
# and the prior bar MACD and RSI compare against are five hours stale.
#
# Cheap on purpose: this touches only the engine universe's intraday series and
# fetches incrementally from the newest stored bar, so it is a handful of
# requests rather than a second full sync. Unconditional because the command
# itself reads the engine's timeframe — a daily engine, or no engine at all,
# prints that there is nothing to refresh and makes no provider call. Deciding
# it here would mean parsing JSON in shell to reach the same answer.
if ! trd sync --intraday-only; then
    echo "intraday refresh failed — continuing with stored bars"
fi

# --- refresh earnings every pass ---------------------------------------------
# Bars settle once a day; earnings dates do not. yfinance publishes some of them
# mid-session, and the blackout can only protect a name whose date is already in
# the database. Observed live on 2026-07-28: the 09:24 sync found no date for BA,
# the engine entered it at 10:25, and the date appeared around noon — the trade
# was taken on the print. This costs a handful of requests because it only checks
# symbols with no known date or one inside the horizon.
if ! trd sync --earnings-only; then
    echo "earnings refresh failed — continuing with stored dates"
fi

# NDJSON because the consumer is promtail, not a human — one event per line, each
# independently queryable in Loki. --notify pushes fills to Telegram; with no
# token configured it degrades to a warning, so an unconfigured cluster still scans.
trd engine scan --ndjson --notify

# --- publish a host-readable snapshot -----------------------------------------
# Written next to the database, NOT to iCloud: this pod runs in a Linux VM and
# cannot see a macOS FileProvider path. deploy/engine-publish.sh (a launchd job
# on the host) copies these two files into iCloud, so the MacBook Air can read
# the engine without ever opening the database — no lock contention, no 23 MB
# binary syncing every five minutes.
#
# Each trd command opens and closes its own connection, so these run cleanly
# after the scan has released the writer lock.
STATUS="${TRD_HOME}/status.txt"
{
    # The build, on the line above the numbers: a stale rollout is otherwise
    # invisible here, and "which code produced this" is the first question worth
    # asking when a rule appears not to have fired.
    echo "trd engine — last scan $(date)  ·  build $(trd version 2>/dev/null || echo unknown)"
    echo
    trd engine positions
    echo
    trd engine report
} > "${STATUS}.tmp" && mv "${STATUS}.tmp" "$STATUS"

# Full engine state (config, signals, positions with stops/targets/trail marks)
# plus every transaction — restorable on another machine with `trd restore`.
BACKUP="${TRD_HOME}/engine-backup.json"
if trd backup "${BACKUP}.tmp" >/dev/null 2>&1; then
    mv "${BACKUP}.tmp" "$BACKUP"
else
    rm -f "${BACKUP}.tmp"
    echo "backup snapshot failed (scan itself succeeded)"
fi
