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

# A busy database is not a failure. If a scan is somehow holding the writer, the
# next tick is ten minutes away and the queue is durable — exiting non-zero here
# would fail a Job and raise an alarm for a condition that resolves itself.
if trd engine apply-queue --notify; then
    exit 0
fi
echo "queue drain deferred (database busy, or the provider was unreachable)"
exit 0
