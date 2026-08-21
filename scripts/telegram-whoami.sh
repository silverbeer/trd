#!/bin/bash
#
# Print the numeric Telegram user ids that have messaged the bot.
#
# The allowlist needs a numeric id, and Telegram does not show you your own.
# This reads it out of getUpdates using the token already in the cluster secret,
# inside a throwaway pod — so the token is never printed, never copied and never
# passes through a terminal, a transcript or a shell history file.
#
#   1. Send any message to your bot in Telegram.
#   2. ./scripts/telegram-whoami.sh
#
# Safe to run while the bot is down. Do NOT run it while the bot Deployment is
# up: getUpdates allows one caller per token and the second gets a 409, which
# would knock the running poller off rather than answer you.
set -euo pipefail

NAMESPACE="${NAMESPACE:-trd}"
IMAGE="${IMAGE:-trd:latest}"

if kubectl get deploy trd-engine-bot -n "$NAMESPACE" &>/dev/null; then
    running=$(kubectl get pods -n "$NAMESPACE" -l component=bot \
        --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')
    if [[ "$running" -gt 0 ]]; then
        echo "The bot is running. Calling getUpdates now would 409 it off its own poll."
        echo "Scale it down first:  kubectl scale deploy/trd-engine-bot -n $NAMESPACE --replicas=0"
        exit 1
    fi
fi

kubectl run "tg-whoami-$RANDOM" -n "$NAMESPACE" --rm -i --restart=Never \
    --image="$IMAGE" --image-pull-policy=Never \
    --overrides='{
  "spec": {
    "restartPolicy": "Never",
    "containers": [{
      "name": "whoami",
      "image": "'"$IMAGE"'",
      "imagePullPolicy": "Never",
      "command": ["python", "-c", "import json,os,urllib.request\ntoken=os.environ.get(\"TELEGRAM_BOT_TOKEN\",\"\").strip()\nif not token:\n    raise SystemExit(\"no TELEGRAM_BOT_TOKEN in the secret\")\nr=json.load(urllib.request.urlopen(f\"https://api.telegram.org/bot{token}/getUpdates\", timeout=20))\nif not r.get(\"ok\"):\n    raise SystemExit(f\"getUpdates failed: {r}\")\nseen={}\nfor u in r.get(\"result\", []):\n    m=u.get(\"message\") or u.get(\"edited_message\") or {}\n    f=m.get(\"from\") or {}\n    if f.get(\"id\"):\n        seen[f[\"id\"]]=(f.get(\"username\") or f.get(\"first_name\") or \"?\", (m.get(\"chat\") or {}).get(\"id\"))\nif not seen:\n    print(\"No messages waiting. Send one to the bot in Telegram, then re-run.\")\n    print(\"(Telegram only retains ~24h of updates, and a running poller consumes them.)\")\nfor uid,(name,chat) in seen.items():\n    print(f\"user_id={uid}  username={name}  chat_id={chat}\")"],
      "envFrom": [{"secretRef": {"name": "trd-engine-telegram"}}]
    }]
  }
}' 2>/dev/null | grep -vE '^pod .* deleted$' || true
