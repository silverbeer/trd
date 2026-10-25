#!/bin/bash
#
# The live engine's broker hand, unattended (SB-1382). Every 10 minutes in the
# session, from launchd (deploy/io.silverbeer.trd.live-runner.plist):
#
#   1. read `trd engine outbox` — the orders the engine has decided on
#   2. review each one (headless claude → review_equity_order); any order_checks
#      → not placed, and the person is told why
#   3. place the clean ones (place_equity_order, the spec's own ref_id). Trade
#      approvals are ON, so each comes back as an approval, and `outbox ack
#      --notify` sends the Telegram message: "approve BUY X ... in the broker's
#      app". The person approves on their phone. That tap is the human gate.
#   4. read back what the broker did with every open intent (orders + approvals)
#      and book it: `trd engine fills --notify`
#
# The model only makes the calls. What came back is read from the session's
# stream-json transcript by deploy/broker_stream.py — the broker's own JSON, never a
# model's retelling — and every order call is checked against the spec that
# asked for it. An order call nobody asked for halts the engine.
#
# Refuses to place when trade approvals are off: with nobody at the keyboard,
# the phone approval is the only person in the loop.
#
# Nothing to do (empty outbox, no open intents) costs nothing: no session runs.
# Every run, busy or idle, writes $TRD_LIVE_HOME/runner.json — the heartbeat
# `trd engine watchdog` reads for a live engine.
#
#   live-runner.sh              one pass (what launchd runs)
#   RH_DRY_RUN=1 live-runner.sh one pass that reviews but never places — the
#                               review is a broker simulation, so this proves
#                               the login, the approval check and the specs
#                               without creating an order
#   live-runner.sh install      pin the runner: copy these scripts and install
#                               trd at the checkout's `main` into ~/.trd-runner.
#                               Re-run after every k3s deploy, never automatic —
#                               code that places orders changes when you say so.
set -u
# shellcheck disable=SC2034 # read by broker-lib.sh
RH_TAG=runner
HERE="$(cd "$(dirname "$0")" && pwd)"

if [[ "${1:-}" == install ]]; then
    set -e
    RUNNER_HOME="${TRD_RUNNER_HOME:-$HOME/.trd-runner}"
    REPO="${TRD_REPO:-$HOME/gitrepos/trd}"
    REF="${2:-main}"
    SHA=$(git -C "$REPO" rev-parse --short "$REF")
    TMP=$(mktemp -d)
    git -C "$REPO" archive "$REF" | tar -x -C "$TMP"
    mkdir -p "$RUNNER_HOME/bin"
    uv venv -q --allow-existing --python 3.14 "$RUNNER_HOME/venv"
    TRD_GIT_SHA="$SHA" uv pip install -q --python "$RUNNER_HOME/venv/bin/python" --reinstall-package trd "$TMP"
    for f in live-runner.sh broker-lib.sh broker_stream.py broker-auth-probe.sh live-runner-settings.json \
        alloc-report.sh; do
        cp "$TMP/deploy/$f" "$RUNNER_HOME/bin/$f"
    done
    chmod +x "$RUNNER_HOME/bin/"*.sh
    echo "$SHA" > "$RUNNER_HOME/VERSION"
    rm -rf "$TMP"
    echo "installed $SHA → $RUNNER_HOME ($("$RUNNER_HOME/venv/bin/trd" version | head -1))"
    exit 0
fi

# shellcheck source=deploy/broker-lib.sh
. "$HERE/broker-lib.sh"

TRD_LIVE_HOME="${TRD_LIVE_HOME:-$HOME/.trd-live}"
TRD="${RUNNER_TRD:-$RUNNER_HOME/venv/bin/trd}"
HEARTBEAT="$TRD_LIVE_HOME/runner.json"
WORK="$RH_STATE_DIR/runner"
LOCK="$RH_STATE_DIR/live-runner.lock"

# No live engine yet: nothing to drain, nothing to say.
[[ -f "$TRD_LIVE_HOME/trd.duckdb" ]] || exit 0
# The session plus a quarter hour: fills land and approvals expire at 16:00,
# and the ingest after the bell is what sweeps the day's leftovers.
# RH_FORCE=1 skips the window — for testing against a throwaway home only.
[[ "${RH_FORCE:-0}" == 1 ]] || rh_window 0930 1615 || exit 0

if ! mkdir "$LOCK" 2>/dev/null; then
    # A pass that died holding the lock must not stop every later one.
    if [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +20 2>/dev/null)" ]]; then
        rh_log "stale lock removed"
        rm -rf "$LOCK" && mkdir "$LOCK" || exit 1
    else
        exit 0
    fi
fi
trap 'rm -rf "$LOCK"' EXIT
mkdir -p "$WORK"

STATUS=ok DETAIL="" PLACED=0 BOOKED=0
heartbeat() {
    "$RH_PY" -c '
import json, sys
from datetime import datetime
status, detail, placed, booked, version, path = sys.argv[1:7]
doc = {"at": datetime.now().isoformat(timespec="seconds"), "status": status,
       "detail": detail, "placed": int(placed), "booked": int(booked), "version": version}
tmp = path + ".tmp"
open(tmp, "w").write(json.dumps(doc) + "\n")
import os; os.replace(tmp, path)
' "$STATUS" "$DETAIL" "$PLACED" "$BOOKED" "$(cat "$RUNNER_HOME/VERSION" 2>/dev/null || echo dev)" "$HEARTBEAT"
}
finish() { heartbeat; rh_log "$STATUS placed=$PLACED booked=$BOOKED ${DETAIL}"; exit "${1:-0}"; }
fail() { STATUS="$1"; DETAIL="$2"; finish 1; }

rh_load_env || fail error "no $RH_ENV_FILE"
[[ -n "${RH_ACCOUNT_NUMBER:-}" ]] || fail error "RH_ACCOUNT_NUMBER missing from $RH_ENV_FILE"
[[ -x "$TRD" ]] || fail error "no runner trd at $TRD — run: deploy/live-runner.sh install"
export TRD_HOME="$TRD_LIVE_HOME"
ACCT="$RH_ACCOUNT_NUMBER"

# trd allows one writer; a scan holding the lock is not a failure, just a wait.
# stdout is the --json document; stderr (warnings, a refused alert) goes to the
# log, so it can never corrupt what is parsed.
trd_retry() {
    local out rc
    for _ in 1 2 3 4 5 6; do
        out=$("$TRD" "$@" 2>>"$RH_LOG"); rc=$?
        if [[ $rc == 0 ]]; then printf '%s' "$out"; return 0; fi
        grep -q DatabaseBusy <<<"$out" || { printf '%s' "$out"; return "$rc"; }
        sleep 10
    done
    printf '%s' "$out"; return 1
}

py() { "$RH_PY" -c "$@"; }

# --- 1. what the engine wants ------------------------------------------------
OUTBOX=$(trd_retry engine outbox --json) || fail error "outbox: ${OUTBOX:0:200}"
printf '%s' "$OUTBOX" > "$WORK/outbox.json"
HALTED=$(py 'import json,sys; print(int(bool(json.load(open(sys.argv[1])).get("halted"))))' "$WORK/outbox.json")
py 'import json,sys; d=json.load(open(sys.argv[1])); json.dump([] if d.get("halted") else d.get("orders") or [], open(sys.argv[2],"w"))' \
    "$WORK/outbox.json" "$WORK/specs.json"
N_SPECS=$(py 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$WORK/specs.json")

# Which intents are out at the broker, for step 4 — read BEFORE placing, so a
# pass never reads back an approval it created seconds ago.
INTENTS=$(trd_retry engine intents --json) || fail error "intents: ${INTENTS:0:200}"
printf '%s' "$INTENTS" > "$WORK/intents.json"
read -r N_OPEN SINCE APPROVAL_IDS < <(py '
import json, sys
from datetime import timedelta
rows = [r["intent"] for r in json.load(open(sys.argv[1]))]
out = [r for r in rows if r["status"] in ("submitted", "awaiting_approval")]
ids = [r["approval_id"] for r in out if r.get("approval_id")]
since = min((r["created_at"][:10] for r in out), default="-")
if since != "-":
    from datetime import date
    since = (date.fromisoformat(since) - timedelta(days=1)).isoformat()
print(len(out), since, json.dumps(ids, separators=(",", ":")))
' "$WORK/intents.json")

if [[ "$N_SPECS" == 0 && "$N_OPEN" == 0 ]]; then
    DETAIL="idle$([[ "$HALTED" == 1 ]] && echo ' (halted)')"
    finish 0
fi

# --- 2. review --------------------------------------------------------------
if [[ "$N_SPECS" -gt 0 ]]; then
    "$RH_PY" "$RH_LIB_DIR/broker_stream.py" args --specs "$WORK/specs.json" --account "$ACCT" --tool review > "$WORK/review-args.json"
    rh_session "$WORK/review.jsonl" "You are a relay with no discretion. Load the tools with ToolSearch (query 'select:mcp__robinhood-trading__get_trade_approval_setting,mcp__robinhood-trading__review_equity_order'). Call get_trade_approval_setting with account_number \"$ACCT\". Then call review_equity_order once for EACH argument object in this JSON array, passing the object exactly as given — do not change, round, add or drop any field: $(cat "$WORK/review-args.json"). Call nothing else, place nothing, then reply DONE." \
        ToolSearch mcp__robinhood-trading__get_trade_approval_setting mcp__robinhood-trading__review_equity_order
    "$RH_PY" "$RH_LIB_DIR/broker_stream.py" review --stream "$WORK/review.jsonl" --specs "$WORK/specs.json" --account "$ACCT" > "$WORK/review.json"

    AUTH=$(py 'import json,sys; print(json.load(open(sys.argv[1]))["auth"] or "")' "$WORK/review.json")
    read -r APPROVALS_ON N_CLEAN UNEXPECTED < <(py '
import json, sys
r = json.load(open(sys.argv[1]))
print({True: "on", False: "off", None: "unknown"}[r["approvals_on"]], len(r["clean"]), len(r["unexpected"]))
' "$WORK/review.json")
    if [[ -n "$AUTH" ]]; then
        rh_note_login FAIL auth "$AUTH"
        fail auth_failed "$AUTH"
    fi
    if [[ "$APPROVALS_ON" != on ]]; then
        rh_note_login FAIL approvals_off "get_trade_approval_setting: $APPROVALS_ON"
        fail approvals_off "approvals $APPROVALS_ON — nothing placed"
    fi
    rh_note_login OK ok approvals=on
    if [[ "$UNEXPECTED" != 0 ]]; then
        WHY=$(py 'import json,sys; print("; ".join(json.load(open(sys.argv[1]))["unexpected"])[:300])' "$WORK/review.json")
        trd_retry engine halt --reason "live runner: unexpected broker call during review: $WHY" >/dev/null
        rh_alert "⛔ trd live engine HALTED by the runner: the review session made a call no order asked for.
$WHY
Nothing was placed. Check, then: trd engine resume"
        fail error "unexpected call in review — halted"
    fi
    # Blocked specs stay pending and lapse at 16:00 on their own; the person is
    # told once per intent, not every ten minutes.
    py '
import json, sys, os
r = json.load(open(sys.argv[1])); seen_path = sys.argv[2]
seen = set(open(seen_path).read().split()) if os.path.exists(seen_path) else set()
for b in r["blocked"]:
    if str(b["intent_id"]) not in seen:
        print(json.dumps(b))
        seen.add(str(b["intent_id"]))
open(seen_path, "w").write("\n".join(sorted(seen)))
' "$WORK/review.json" "$WORK/blocked.seen" | while read -r line; do
        MSG=$(py '
import json, sys
b = json.loads(sys.argv[1])
print("%s %s %s sh (intent %s) — %s" % (b["side"].upper(), b["symbol"], b["quantity"], b["intent_id"], b["why"]))
' "$line")
        rh_alert "💵 LIVE: NOT placed — the broker's review flagged it
$MSG
It stays in the outbox and lapses at 16:00. Nothing for you to approve."
        rh_log "blocked: $MSG"
    done

    # --- 3. place ------------------------------------------------------------
    if [[ "${RH_DRY_RUN:-0}" == 1 ]]; then
        DETAIL="dry run: $N_CLEAN of $N_SPECS reviewed clean, nothing placed"
        N_CLEAN=0
    fi
    if [[ "$N_CLEAN" -gt 0 ]]; then
        py '
import json, sys
clean = set(json.load(open(sys.argv[1]))["clean"])
json.dump([s for s in json.load(open(sys.argv[2])) if s["intent_id"] in clean], open(sys.argv[3], "w"))
' "$WORK/review.json" "$WORK/specs.json" "$WORK/clean.json"
        "$RH_PY" "$RH_LIB_DIR/broker_stream.py" args --specs "$WORK/clean.json" --account "$ACCT" --tool place > "$WORK/place-args.json"
        rh_session "$WORK/place.jsonl" "You are a relay with no discretion. Load mcp__robinhood-trading__place_equity_order with ToolSearch (query 'select:mcp__robinhood-trading__place_equity_order'). Call place_equity_order once for EACH argument object in this JSON array, passing the object exactly as given, ref_id included — do not change, round, add or drop any field, and never invent a ref_id: $(cat "$WORK/place-args.json"). These orders were already reviewed; the customer approves each in the Robinhood app. If a call fails, do not retry it with different arguments. Call nothing else, then reply DONE." \
            ToolSearch mcp__robinhood-trading__place_equity_order
        "$RH_PY" "$RH_LIB_DIR/broker_stream.py" placed --stream "$WORK/place.jsonl" --specs "$WORK/clean.json" --account "$ACCT" > "$WORK/placed.json"

        # Record every answer, then deal with surprises. An ack that fails is
        # retried next pass from the log, never by placing again.
        while IFS=$'\t' read -r intent kind value; do
            case "$kind" in
                approval) flag=--approval-id ;;
                order) flag=--broker-order-id ;;
                *) flag=--error ;;
            esac
            if out=$(trd_retry engine outbox ack "$intent" "$flag" "$value" --notify --json); then
                [[ "$kind" == error ]] || PLACED=$((PLACED + 1))
                rh_log "ack $intent $kind ${value:0:80}"
            else
                rh_log "ACK FAILED $intent $kind $value: ${out:0:200}"
                rh_alert "⚠ trd live runner: placed but could not record it — intent $intent, $kind $value. Do NOT re-place; run: trd engine outbox ack $intent $flag '$value'"
            fi
        done < <(py '
import json, sys
for a in json.load(open(sys.argv[1]))["acks"]:
    print(a["intent_id"], a["kind"], a["value"].replace("\t", " ").replace("\n", " "), sep="\t")
' "$WORK/placed.json")

        UNEXPECTED=$(py 'import json,sys; print("; ".join(json.load(open(sys.argv[1]))["unexpected"])[:300])' "$WORK/placed.json")
        if [[ -n "$UNEXPECTED" ]]; then
            trd_retry engine halt --reason "live runner: order call no spec asked for: $UNEXPECTED" >/dev/null
            rh_alert "⛔ trd live engine HALTED by the runner: an order call did not match any outbox spec.
$UNEXPECTED
If an approval for it is waiting in the broker's app, DECLINE it. Then check and: trd engine resume"
            fail error "unexpected order call — halted"
        fi
        PLACE_AUTH=$(py 'import json,sys; print(json.load(open(sys.argv[1]))["auth"] or "")' "$WORK/placed.json")
        if [[ -n "$PLACE_AUTH" ]]; then
            rh_note_login FAIL auth "$PLACE_AUTH"
            fail auth_failed "$PLACE_AUTH"
        fi
    fi
fi

# --- 4. read back and book ---------------------------------------------------
if [[ "$N_OPEN" -gt 0 ]]; then
    APPROVAL_ARG=""
    [[ "$APPROVAL_IDS" != "[]" ]] && APPROVAL_ARG=" Then call get_trade_approvals with account_number \"$ACCT\" and approval_ids $APPROVAL_IDS."
    rh_session "$WORK/fills.jsonl" "You are a relay with no discretion. Load the tools with ToolSearch (query 'select:mcp__robinhood-trading__get_equity_orders,mcp__robinhood-trading__get_trade_approvals'). Call get_equity_orders with account_number \"$ACCT\", created_at_gte \"$SINCE\" and agent_initiated true.$APPROVAL_ARG Call nothing else, then reply DONE." \
        ToolSearch mcp__robinhood-trading__get_equity_orders mcp__robinhood-trading__get_trade_approvals
    if ! "$RH_PY" "$RH_LIB_DIR/broker_stream.py" snapshot --stream "$WORK/fills.jsonl" --account "$ACCT" > "$WORK/snapshot.json"; then
        AUTH=$(py 'import json,sys; print(json.load(open(sys.argv[1])).get("auth") or "")' "$WORK/snapshot.json")
        if [[ -n "$AUTH" ]]; then
            rh_note_login FAIL auth "$AUTH"
            fail auth_failed "$AUTH"
        fi
        fail error "no usable orders read for the fill snapshot"
    fi
    rh_note_login OK ok "orders read"
    if FILLS=$(trd_retry engine fills "$WORK/snapshot.json" --notify --json); then
        BOOKED=$(py 'import json,sys; print(sum(1 for a in json.loads(sys.argv[1]).get("applied", []) if a.get("to_status") == "filled"))' "$FILLS" 2>/dev/null || echo 0)
        # Kept two weeks: the broker's own record of what trd booked from.
        cp "$WORK/snapshot.json" "$WORK/snapshot-$(date +%Y%m%dT%H%M%S).json"
        find "$WORK" -name 'snapshot-*.json' -mtime +14 -delete
    else
        # A conflict exits non-zero and has already alerted (--notify); say so
        # in the heartbeat rather than hiding it.
        STATUS=error DETAIL="fills: ${FILLS:0:200}"
    fi
fi

finish 0
