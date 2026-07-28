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

# NDJSON because the consumer is promtail, not a human — one event per line, each
# independently queryable in Loki. --notify pushes fills to Telegram; with no
# token configured it degrades to a warning, so an unconfigured cluster still scans.
exec trd engine scan --ndjson --notify
