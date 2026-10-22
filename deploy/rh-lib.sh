# shellcheck shell=bash
#
# Shared by deploy/live-runner.sh and deploy/rh-auth-probe.sh. Sourced, not run.
#
# Both scripts reach the Robinhood MCP the only way it can be reached: a headless
# `claude -p` in the trd checkout, where the server is configured (local scope)
# and its OAuth sign-in lives. Both alert through the same Telegram bot to the
# private alert chat, and share one login state so a lapsed sign-in is one
# message, not one from each.

RH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER_HOME="${TRD_RUNNER_HOME:-$HOME/.trd-runner}"
RH_ENV_FILE="${RH_ENV_FILE:-$HOME/.config/trd/live.env}"
RH_STATE_DIR="${RH_STATE_DIR:-$HOME/.local/state/trd}"
RH_LOG="${RH_LOG:-$HOME/Library/Logs/trd-live-runner.log}"
# Where `claude` finds the robinhood-trading server: it is local-scope config,
# keyed to this directory. Only its MCP config is used — never its code.
TRD_REPO="${TRD_REPO:-$HOME/gitrepos/trd}"
RH_MODEL="${RH_MODEL:-sonnet}"
RH_CLAUDE_TIMEOUT="${RH_CLAUDE_TIMEOUT:-300}"
RH_REPEAT_HOURS="${RH_REPEAT_HOURS:-3}"
# The parser needs nothing beyond the stdlib; the runner's venv python is
# preferred only because it is a known version.
RH_PY="${RH_PY:-$RUNNER_HOME/venv/bin/python}"
[[ -x "$RH_PY" ]] || RH_PY=python3

mkdir -p "$RH_STATE_DIR" "$(dirname "$RH_LOG")"

rh_log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') [$RH_TAG] $*" >> "$RH_LOG"; }

# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (the channel, for fill reports),
# TRD_BOT_ALLOWED_USER_IDS or TRD_LIVE_ALERT_CHAT_ID (the private alert chat) and
# RH_ACCOUNT_NUMBER. Exported, because `trd ... --notify` reads the same names.
rh_load_env() {
    if [[ ! -r "$RH_ENV_FILE" ]]; then
        rh_log "no $RH_ENV_FILE — cannot alert or name the account"
        return 1
    fi
    set -a
    # shellcheck disable=SC1090
    . "$RH_ENV_FILE"
    set +a
    RH_ALERT_CHAT="${TRD_LIVE_ALERT_CHAT_ID:-}"
    if [[ -z "$RH_ALERT_CHAT" ]]; then
        RH_ALERT_CHAT=$(printf '%s' "${TRD_BOT_ALLOWED_USER_IDS:-}" | cut -d, -f1 | tr -d ' ')
    fi
}

# To the private alert chat, never the channel: these are calls to act on real
# money. The token goes to curl on stdin as config — not argv (ps), not the log.
rh_alert() {
    if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${RH_ALERT_CHAT:-}" ]]; then
        rh_log "ALERT NOT SENT (no token or alert chat): ${1%%$'\n'*}"
        return 1
    fi
    local code
    code=$(printf 'url = "https://api.telegram.org/bot%s/sendMessage"\n' "$TELEGRAM_BOT_TOKEN" \
        | curl -s -m 20 -o /dev/null -w '%{http_code}' -K - \
            --data-urlencode "chat_id=$RH_ALERT_CHAT" \
            --data-urlencode "text=$1" \
            --data "disable_web_page_preview=true")
    if [[ "$code" != 200 ]]; then
        rh_log "ALERT FAILED (HTTP $code): ${1%%$'\n'*}"
        return 1
    fi
}

# Weekday and FROM <= HHMM (New York) < TO.
rh_window() {
    [[ $(TZ=America/New_York date +%u) -le 5 ]] || return 1
    local hm
    hm=$(TZ=America/New_York date +%H%M)
    (( 10#$hm >= 10#$1 && 10#$hm < 10#$2 ))
}

# rh_session OUT_FILE PROMPT TOOL... — one headless session, transcript to OUT_FILE.
# Loads ONLY local settings (the project's `ask` cannot prompt with nobody there)
# plus the runner's own allow/deny list. No `timeout` on macOS: perl's alarm
# kills a session that hangs waiting on a browser sign-in it can never finish.
rh_session() {
    local out="$1" prompt="$2"
    shift 2
    local tools
    tools=$(IFS=,; echo "$*")
    (cd "$TRD_REPO" && env -u CLAUDECODE perl -e "alarm $RH_CLAUDE_TIMEOUT; exec @ARGV" \
        claude -p "$prompt" \
        --model "$RH_MODEL" \
        --setting-sources local \
        --settings "$RH_LIB_DIR/live-runner-settings.json" \
        --allowedTools "$tools" \
        --max-turns 20 \
        --output-format stream-json --verbose \
        < /dev/null > "$out" 2>&1)
}

# rh_note_login OK|FAIL KIND DETAIL — the one login state both scripts share.
# Alerts on the change, repeats at most every RH_REPEAT_HOURS while the market
# is open, and says when it works again.
rh_note_login() {
    local status="$1" kind="$2" detail="$3"
    local state="$RH_STATE_DIR/rh-auth.state" prev=OK last=0 now what
    [[ -r "$state" ]] && read -r prev last < "$state"
    [[ -n "$prev" && -n "$last" ]] || { prev=OK; last=0; }
    now=$(date +%s)
    if [[ "$status" == FAIL ]]; then
        if [[ "$kind" == approvals_off ]]; then
            what="Trade approvals are OFF on the Robinhood agent account, so the live runner will not place orders. Turn them back on: Robinhood app → Agent Settings → Safety Controls."
        else
            what="The live engine cannot place orders until Robinhood is signed in again. On the mini: cd ~/gitrepos/trd, run claude, type /mcp, pick robinhood-trading, authenticate."
        fi
        if [[ "$prev" != FAIL ]]; then
            rh_alert "🔴 trd: Robinhood login check FAILED
$what
Detail: $detail" && last=$now
        elif rh_window 0930 1600 && (( now - last >= RH_REPEAT_HOURS * 3600 )); then
            rh_alert "🔴 trd: Robinhood login still failing
$what
Detail: $detail" && last=$now
        fi
    elif [[ "$prev" == FAIL ]]; then
        rh_alert "🟢 trd: Robinhood login works again — the live engine can place orders."
        last=0
    fi
    echo "$status $last" > "$state"
}
