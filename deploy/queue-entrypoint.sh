#!/bin/sh
# Drain the chat command queue when the scan is not running.
#
# The queue is normally drained inside the scan, which is the process that
# safely holds DuckDB's single writer. That is right during the session and
# useless outside it: engine-entrypoint.sh exits at its market-hours guard long
# before it reaches the drain, and the scan CronJob does not fire in the evening
# at all. A command typed at 17:23 waited sixteen hours.
#
# This runs every ten minutes, all week, and stays out of the scan's way.

set -eu

# The scan owns the queue during the session — it drains before it scans, so a
# name added from chat is in the universe for the very next pass. Overlapping
# here would be worse than waiting: two CronJobs are not covered by each other's
# concurrencyPolicy, so a drain that grabbed the writer as a scan started would
# fail THE SCAN. The window is deliberately wider than 09:30-16:00 at both ends.
if [ "${TRD_QUEUE_FORCE:-0}" != "1" ]; then
    dow=$(TZ=America/New_York date +%u)
    hour=$(TZ=America/New_York date +%-H)
    minute=$(TZ=America/New_York date +%-M)
    now=$((hour * 100 + minute))
    if [ "$dow" -le 5 ] && [ "$now" -ge 925 ] && [ "$now" -le 1605 ]; then
        echo "in the session window — the scan drains the queue; nothing to do"
        exit 0
    fi
fi

# --- settle the day's bars after the close (SB-1411) ---------------------------
# engine-entrypoint.sh syncs once, before the first pass; every later scan reads
# the quote and stores no bar. So the bar dated today is the MORNING's, and the
# 16:16 report, the 16:20 live report and the 16:31 review all read it as the
# close: on 2026-10-23 PANW closed 380.55 and every report said 355.01, +0.8% on
# a +8% day. The next morning's sync overwrites it, so history was never wrong —
# only the evening, which is when anyone reads it.
#
# Once per weekday, on the first tick past 16:05 (16:10, ahead of the reports).
# The stamp is written only when every symbol is current, so a name the provider
# has not finalised yet is retried ten minutes later rather than left behind.
dow=$(TZ=America/New_York date +%u)
hour=$(TZ=America/New_York date +%-H)
minute=$(TZ=America/New_York date +%-M)
now=$((hour * 100 + minute))
CLOSE_STAMP="${TRD_HOME}/.close-sync"
today=$(TZ=America/New_York date +%F)
if [ "$dow" -le 5 ] && [ "$now" -gt 1605 ] \
    && [ "$(cat "$CLOSE_STAMP" 2>/dev/null || true)" != "$today" ]; then
    if trd sync --require-current; then
        echo "$today" > "$CLOSE_STAMP"
        echo "close sync done — today's bars are the close"
    else
        echo "close sync incomplete (busy, unreachable, or a symbol not final) — retrying next tick"
    fi
fi

# A busy database is not a failure. If a scan is somehow holding the writer, the
# next tick is ten minutes away and the queue is durable — exiting non-zero here
# would fail a Job and raise an alarm for a condition that resolves itself.
if trd engine apply-queue --notify; then
    exit 0
fi
echo "queue drain deferred (database busy, or the provider was unreachable)"
exit 0
