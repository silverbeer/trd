#!/bin/bash
#
# The IRA's daily performance message (SB-1384), from launchd on the
# mini, weekdays after the close (deploy/io.silverbeer.trd.alloc-report.plist).
#
# `trd alloc report` opens the database READ-ONLY and stores no quotes: the
# portfolio database syncs through iCloud between two Macs, and an unattended
# job must never be a second writer on it.
#
# Telegram settings come from the same file as the live runner
# (~/.config/trd/live.env, chmod 600): TELEGRAM_BOT_TOKEN, and
# TRD_LIVE_ALERT_CHAT_ID or TRD_BOT_ALLOWED_USER_IDS — the message goes to your
# private chat, never the channel.
set -u
ENV_FILE="${RH_ENV_FILE:-$HOME/.config/trd/live.env}"
export TRD_HOME="${TRD_HOME:-$HOME/.trd-portfolio}"
# The pinned build (deploy/live-runner.sh install), so the report changes when
# you re-install, not when the checkout moves.
TRD="${TRD_BIN:-$HOME/.trd-runner/venv/bin/trd}"
LOG="${ALLOC_REPORT_LOG:-$HOME/Library/Logs/trd-alloc-report.log}"

stamp() { date '+%Y-%m-%dT%H:%M:%S%z'; }
if [[ ! -r "$ENV_FILE" ]]; then
    echo "$(stamp) no $ENV_FILE — cannot send" >> "$LOG"
    exit 1
fi
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a
"$TRD" alloc report --notify ${ALLOC_ACCOUNT:+--account "$ALLOC_ACCOUNT"} >> "$LOG" 2>&1
rc=$?
echo "$(stamp) exit $rc" >> "$LOG"
if [[ "$rc" != 0 ]]; then
    # A failed report used to be a line in this log and nothing else: on
    # 2026-11-03 the report simply did not arrive (SB-1524). Say so, in the same
    # private chat the report goes to. No log text in the message — the log is
    # local, and the alert only has to say where to look.
    hint=""
    if tail -n 20 "$LOG" | grep -qi "behind"; then
        hint=" The database is behind the installed build: run deploy/live-runner.sh install."
    fi
    # shellcheck disable=SC2034 # read by broker-lib.sh
    RH_TAG=alloc RH_LOG="$LOG" RH_ENV_FILE="$ENV_FILE"
    # shellcheck source=deploy/broker-lib.sh
    . "$(dirname "$0")/broker-lib.sh"
    rh_load_env && rh_alert "⚠️ Today's IRA report failed (exit $rc) — see $LOG on the mini.$hint"
fi
exit "$rc"
