#!/bin/bash
#
# Is the broker's MCP still signed in? Checked the way the live runner uses it:
# a headless `claude -p`, nobody at the keyboard. Hourly, from launchd
# (deploy/io.silverbeer.trd.broker-auth-probe.plist).
#
# When the sign-in lapses, every order the live engine wants sits in the outbox
# until 16:00: buys expire harmlessly, but an exit nobody places leaves a real
# position unmanaged. So a failure is a Telegram message to the private alert
# chat (shared state with the runner — one alert, not two), a reminder at most
# every RH_REPEAT_HOURS while the market is open, and an all-clear.
#
# It also measures how long a sign-in lasts (SB-1382): every run appends
# `OK|FAIL <kind> <detail>` to the log, and the gaps between a FAIL and the
# re-login before it are the answer.
#
# Read-only. Approvals OFF counts as a failure: the runner will not place then.
#
# Test the alert path without a real failure:  RH_PROBE_FORCE_FAIL=1 broker-auth-probe.sh
set -u
# shellcheck disable=SC2034 # read by broker-lib.sh
RH_TAG=probe
# shellcheck source=deploy/broker-lib.sh
. "$(cd "$(dirname "$0")" && pwd)/broker-lib.sh"

rh_load_env || exit 1
if [[ -z "${RH_ACCOUNT_NUMBER:-}" ]]; then
    rh_log "RH_ACCOUNT_NUMBER missing from $RH_ENV_FILE"
    exit 1
fi

if [[ "${RH_PROBE_FORCE_FAIL:-0}" == 1 ]]; then
    RESULT='{"status": "FAIL", "kind": "auth", "detail": "forced test (RH_PROBE_FORCE_FAIL=1) - nothing is wrong"}'
else
    STREAM="$RH_STATE_DIR/probe.jsonl"
    rh_session "$STREAM" \
        "Load mcp__robinhood-trading__get_trade_approval_setting with ToolSearch (query 'select:mcp__robinhood-trading__get_trade_approval_setting'), call it once with account_number \"$RH_ACCOUNT_NUMBER\", then reply DONE. Call nothing else." \
        ToolSearch mcp__robinhood-trading__get_trade_approval_setting
    RESULT=$("$RH_PY" "$RH_LIB_DIR/broker_stream.py" probe --stream "$STREAM")
fi

read -r STATUS KIND DETAIL < <("$RH_PY" -c '
import json, sys
r = json.loads(sys.argv[1])
print(r["status"], r["kind"], r["detail"].replace("\n", " ")[:200])
' "$RESULT")
rh_log "$STATUS $KIND $DETAIL"
rh_note_login "$STATUS" "$KIND" "$DETAIL"
[[ "$STATUS" == OK ]]
