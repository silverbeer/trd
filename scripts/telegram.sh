#!/bin/bash
#
# Talk to the Telegram bot API without a pod, a heredoc, or the token on screen.
#
#   ./scripts/telegram.sh check              # is the token live? which bot is it?
#   ./scripts/telegram.sh send "text"        # send to TELEGRAM_CHAT_ID
#   ./scripts/telegram.sh whoami             # numeric user ids that messaged it
#
# The token is resolved in this order and never printed:
#
#   1. $TELEGRAM_BOT_TOKEN                   explicit beats implicit
#   2. 1Password                             op://Personal/Telegram Bot Tokens/trd-engine-bot
#   3. the cluster secret                    trd-engine-telegram in namespace trd
#
# 1Password before the cluster on purpose: a laptop that can reach the vault does
# not need kubectl, and this stays useful when the cluster is down — which is one
# of the times you most want to ask whether the token still works.
set -euo pipefail

OP_REF="${TRD_TELEGRAM_OP_REF:-op://Personal/Telegram Bot Tokens/trd-engine-bot}"
NAMESPACE="${NAMESPACE:-trd}"
API="https://api.telegram.org"

die() { echo "$*" >&2; exit 1; }

resolve_token() {
    if [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]]; then
        echo "token source: environment" >&2
        printf '%s' "$TELEGRAM_BOT_TOKEN"
        return
    fi
    if command -v op >/dev/null 2>&1; then
        # `op read` prompts for unlock if the session has expired, which can take
        # a while but is not a failure — do not race it with a timeout.
        local value
        if value=$(op read "$OP_REF" 2>/dev/null) && [[ -n "$value" ]]; then
            echo "token source: 1Password ($OP_REF)" >&2
            printf '%s' "$value"
            return
        fi
    fi
    if command -v kubectl >/dev/null 2>&1; then
        local value
        if value=$(kubectl get secret trd-engine-telegram -n "$NAMESPACE" \
            -o jsonpath='{.data.TELEGRAM_BOT_TOKEN}' 2>/dev/null | base64 -d 2>/dev/null) \
            && [[ -n "$value" ]]; then
            echo "token source: cluster secret trd-engine-telegram" >&2
            printf '%s' "$value"
            return
        fi
    fi
    die "No token. Set TELEGRAM_BOT_TOKEN, unlock 1Password (op signin), or point kubectl at the cluster."
}

resolve_chat() {
    if [[ -n "${TELEGRAM_CHAT_ID:-}" ]]; then
        printf '%s' "$TELEGRAM_CHAT_ID"
        return
    fi
    kubectl get secret trd-engine-telegram -n "$NAMESPACE" \
        -o jsonpath='{.data.TELEGRAM_CHAT_ID}' 2>/dev/null | base64 -d 2>/dev/null \
        || die "No chat id. Set TELEGRAM_CHAT_ID."
}

# Every call goes through python's stdlib rather than curl: the token is in the
# URL, so it must not reach a shell trace, a history file, or `ps`.
call() { python3 "$(dirname "$0")/_telegram_call.py" "$1" "$2" "$3"; }

bot_is_polling() {
    kubectl get pods -n "$NAMESPACE" -l component=bot \
        --field-selector=status.phase=Running --no-headers 2>/dev/null | grep -q .
}

cmd="${1:-}"
[[ -n "$cmd" ]] || die "Usage: $0 {check|send <text>|whoami}"
shift || true

TOKEN=$(resolve_token)

case "$cmd" in
    check)  call "$TOKEN" getMe '{}' ;;
    send)
        [[ $# -gt 0 ]] || die "send needs a message"
        chat=$(resolve_chat)
        call "$TOKEN" sendMessage "$(python3 -c '
import json,sys; print(json.dumps({"chat_id": sys.argv[1], "text": sys.argv[2]}))' "$chat" "$*")"
        ;;
    whoami)
        # getUpdates allows one caller per token. Asking while the deployed bot
        # polls would take its slot and 409 it off, so this refuses rather than
        # trading a diagnostic for an outage.
        if bot_is_polling; then
            die "The bot is polling. Calling getUpdates now would 409 it off.
Scale it down first:  kubectl scale deploy/trd-engine-bot -n $NAMESPACE --replicas=0"
        fi
        call "$TOKEN" getUpdates '{"allowed_updates":["message"]}'
        ;;
    *) die "Unknown command '$cmd'. Try check, send or whoami." ;;
esac
