#!/bin/bash
#
# Deploy the trd engine to k3s.
#
# Usage:
#   ./scripts/deploy-k3s.sh                 # build image, import, apply manifests
#   ./scripts/deploy-k3s.sh --skip-build    # apply manifests against the existing image
#   ./scripts/deploy-k3s.sh --test          # apply, then run one scan now (ignores market hours)
#   ./scripts/deploy-k3s.sh --day           # the day-mode engine (~/.trd-day) instead
#   ./scripts/deploy-k3s.sh --swing20       # the paper copy of the live engine (~/.trd-swing20)
#   ./scripts/deploy-k3s.sh --live          # the LIVE engine (~/.trd-live, real money; never seeded here)
#   ./scripts/deploy-k3s.sh --bot           # the Telegram command bot (one Deployment)
#   ./scripts/deploy-k3s.sh --report        # the post-market report (one CronJob, every engine)
#   ./scripts/deploy-k3s.sh --review        # the nightly decision review (one CronJob, every engine)
#
# Engines run side by side: they share the image, the namespace and the
# optional Telegram secret, and differ only in which database they mount. The
# manifest is rewritten per deployment rather than duplicated, so there is one
# file to keep correct.
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

IMAGE_NAME="trd"
IMAGE_TAG="latest"
IMAGE_FULL="${IMAGE_NAME}:${IMAGE_TAG}"
NAMESPACE="trd"

SKIP_BUILD=false
RUN_TEST=false
DAY_MODE=false
SWING20_MODE=false
LIVE_MODE=false
BOT_MODE=false
REPORT_MODE=false
REVIEW_MODE=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-build) SKIP_BUILD=true; shift ;;
        --test) RUN_TEST=true; shift ;;
        --day) DAY_MODE=true; shift ;;
        --swing20) SWING20_MODE=true; shift ;;
        --live) LIVE_MODE=true; shift ;;
        --bot) BOT_MODE=true; shift ;;
        --report) REPORT_MODE=true; shift ;;
        --review) REVIEW_MODE=true; shift ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--skip-build] [--test] [--day | --swing20 | --live] [--bot] [--report] [--review]"
            exit 1
            ;;
    esac
done

PICKED=0
for flag in "$DAY_MODE" "$SWING20_MODE" "$LIVE_MODE"; do
    [[ "$flag" == true ]] && PICKED=$((PICKED + 1))
done
if [[ "$PICKED" -gt 1 ]]; then
    echo "--day, --swing20 and --live name different engines; pick one."
    exit 1
fi

# A forced scan proves a deploy on paper. On real money it proves nothing the
# paper twin (--swing20) does not, and an exit it fires is a real sell intent
# written off an after-hours quote.
if [[ "$LIVE_MODE" == true && "$RUN_TEST" == true ]]; then
    echo "--test is refused on --live. Prove the image with: $0 --skip-build --test --swing20"
    exit 1
fi

# Which engine this invocation deploys. The name is load-bearing: it becomes the
# CronJob name, so without it a second deployment would REPLACE the first and
# silently repoint the running engine at the other database.
#
# ENGINE_LABEL is a different thing: what the engine calls itself in Telegram
# (TRD_ENGINE_LABEL), and the same word the bot, the report and the review use
# for its home. It is not derived from ENGINE_NAME because the first engine's
# objects are called trd-engine, and renaming them would orphan the running
# CronJobs — while "trd-engine" in chat named nothing anyone types.
#
# SCAN_NOTIFY=0 makes the scan run without --notify (deploy/engine-entrypoint.sh).
if [[ "$DAY_MODE" == true ]]; then
    ENGINE_NAME="${ENGINE_NAME:-trd-day}"
    ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-day}"
    ENGINE_LABEL="day"
    SCAN_NOTIFY=1
    MODE_FLAG=" --day"
    # Load-bearing: an engine seeded without this is a swing engine wearing a day
    # engine's name — it would carry positions overnight, the one thing day mode
    # exists to prevent.
    INIT_FLAGS=(--flat-at "${FLAT_AT:-1555}")
elif [[ "$SWING20_MODE" == true ]]; then
    # The paper copy of the future live engine: 5 slots x $20 on $100 of capital,
    # T+1 settled-cash ledger on. Fills are NOT pushed — a third engine posting
    # every fill to the shared chat is the noise the daily report replaced; it
    # still appears in the report and the review, and still answers chat.
    ENGINE_NAME="${ENGINE_NAME:-trd-swing20}"
    ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-swing20}"
    ENGINE_LABEL="swing20"
    SCAN_NOTIFY=0
    MODE_FLAG=" --swing20"
    # The swing engine's strategies and universe are deliberately NOT read here:
    # this script never opens another engine's database. Seed ~/.trd-swing20 by
    # hand first, passing swing's --strategies and --symbols (k3s/trd-engine/
    # README.md, "swing20"). The seed step below only runs on an unconfigured
    # home, and on its own it would take the default ten names and every rule.
    INIT_FLAGS=(--account swing20 --size 20 --max 5 --capital 100)
elif [[ "$LIVE_MODE" == true ]]; then
    # The live engine: same rules as swing20, trading the real Robinhood cash
    # account. Its scan writes order intents, never trades; an agent session
    # places them. Fills are not pushed by the scan (there are none) — but the
    # scan keeps --notify, because that is what sends each new intent to the
    # private alert chat (TRD_BOT_ALLOWED_USER_IDS, from the same secret).
    #
    # NEVER seeded here. `engine init --live` takes an explicit real account,
    # capital and the swing universe; guessing any of them would put money on a
    # config nobody chose. Step 0 refuses an unconfigured or paper home instead.
    ENGINE_NAME="${ENGINE_NAME:-trd-live}"
    ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-live}"
    ENGINE_LABEL="💵 live"
    SCAN_NOTIFY=1
    MODE_FLAG=" --live"
    INIT_FLAGS=()
else
    ENGINE_NAME="${ENGINE_NAME:-trd-engine}"
    # The engine's own database. Deliberately NOT your real trd database: this
    # one holds a paper account and its universe's price history, nothing else.
    ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-engine}"
    ENGINE_LABEL="swing"
    SCAN_NOTIFY=1
    MODE_FLAG=""
    INIT_FLAGS=()
fi

CURRENT_CONTEXT=$(kubectl config current-context)
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}K3s Deployment: ${ENGINE_NAME}${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "Context:     ${YELLOW}${CURRENT_CONTEXT}${NC}"
if [[ "$LIVE_MODE" == true ]]; then
    echo -e "Engine DB:   ${RED}${ENGINE_HOME}  (LIVE — REAL MONEY: writes order intents)${NC}"
else
    echo -e "Engine DB:   ${YELLOW}${ENGINE_HOME}${NC}  (paper only)"
fi
echo -e "Label:       ${YELLOW}${ENGINE_LABEL}${NC}  (fill pushes $([[ "$SCAN_NOTIFY" == 1 ]] && echo on || echo off))"
echo -e "Your real DB is not touched by any of this."
echo ""

# The engine writes trades. Deploying it to the wrong cluster is not a no-op, so
# make the operator look at the context name before anything happens.
read -r -p "Deploy to this context? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 1; }
echo ""

# --- Step 0: make sure the database the pod mounts actually has an engine in it.
# A pod started against an unseeded directory just fails every five minutes with
# "No engine configured", which is a slow and confusing way to find this out.
if ! command -v trd &> /dev/null; then
    echo -e "${RED}❌ trd is not on PATH. Run: uv tool install --editable .${NC}"
    exit 1
fi

# Seed ONLY when the engine genuinely is not configured. The previous check
# reseeded whenever a command exited non-zero, which a momentarily locked
# database also does — and `trd engine init` rewrites the config: it would reset
# the universe to the default ten and, on a day engine, drop flat_at_minute,
# leaving something that holds positions overnight. `--json` makes the two cases
# distinguishable, since a busy database reports DatabaseBusyError.
needs_seed() {
    [[ -f "$ENGINE_HOME/trd.duckdb" ]] || return 0
    local err
    err=$(TRD_HOME="$ENGINE_HOME" trd engine status --json 2>/dev/null \
          | grep -o '"error":"[^"]*"' || true)
    [[ "$err" == '"error":"TrdError"' ]]
}

if [[ "$LIVE_MODE" == true ]]; then
    # Refuse rather than seed (see the --live block above), and refuse a paper
    # home too: the CronJob would be named and labelled live while trading paper.
    if needs_seed; then
        echo -e "${RED}✗ ${ENGINE_HOME} has no engine.${NC} Create it by hand first:"
        echo "    TRD_HOME=${ENGINE_HOME} trd init"
        echo "    TRD_HOME=${ENGINE_HOME} trd engine init --live --account <real account> \\"
        echo "        --capital 100 --size 20 --max 5 --symbols ... --strategies ..."
        echo "    TRD_HOME=${ENGINE_HOME} trd sync --full"
        exit 1
    fi
    if ! TRD_HOME="$ENGINE_HOME" trd engine status --json 2>/dev/null | grep -q '"mode": *"live"'; then
        echo -e "${RED}✗ ${ENGINE_HOME} is not a live engine${NC} (or its database is busy — retry)."
        exit 1
    fi
    echo -e "${GREEN}✅ Live engine configured${NC}"
elif needs_seed; then
    echo -e "${YELLOW}🌱 Seeding the engine database at ${ENGINE_HOME}...${NC}"
    echo -e "${BLUE}   (a separate paper database — your real trd data is elsewhere)${NC}"
    TRD_HOME="$ENGINE_HOME" trd init
    TRD_HOME="$ENGINE_HOME" trd engine init "${INIT_FLAGS[@]}"
    echo -e "${YELLOW}   Downloading 2 years of daily bars (the rules need 200)...${NC}"
    TRD_HOME="$ENGINE_HOME" trd sync --full
    echo -e "${GREEN}✅ Engine database ready${NC}"
else
    echo -e "${GREEN}✅ Engine database already seeded${NC}"
fi
echo ""

# What is already deployed, asked before we replace it. A --skip-build deploy that
# silently reuses a months-old image is how a day engine ends up running without
# its session-close rule; printing both SHAs makes a no-op deploy visible.
RUNNING_VERSION=$(kubectl run "trd-version-$RANDOM" -n "$NAMESPACE" --image="$IMAGE_FULL" \
    --image-pull-policy=Never --rm -i --restart=Never --command -- trd version 2>/dev/null \
    | tr -d '\r' | head -1 || true)
BUILD_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "")

if [[ "$SKIP_BUILD" == false ]]; then
    echo -e "${YELLOW}📦 Building image${NC} (${BUILD_SHA:-no git sha})..."
    docker build --build-arg "TRD_GIT_SHA=${BUILD_SHA}" -t "$IMAGE_FULL" .
    echo -e "${GREEN}✅ Built${NC}"
    echo ""

    echo -e "${YELLOW}📥 Importing into k3s...${NC}"
    if [[ "$CURRENT_CONTEXT" == "rancher-desktop" ]]; then
        if command -v nerdctl &> /dev/null; then
            docker save "$IMAGE_FULL" | nerdctl -n k8s.io load
        elif command -v ctr &> /dev/null; then
            docker save "$IMAGE_FULL" | ctr -n k8s.io images import -
        else
            echo -e "${RED}❌ Neither nerdctl nor ctr found.${NC}"; exit 1
        fi
    else
        docker save "$IMAGE_FULL" -o /tmp/${IMAGE_NAME}.tar
        sudo k3s ctr images import /tmp/${IMAGE_NAME}.tar
        rm /tmp/${IMAGE_NAME}.tar
    fi
    echo -e "${GREEN}✅ Imported${NC}"
    echo ""
else
    echo -e "${YELLOW}⏭️  Skipping build — the cluster keeps whatever image it has.${NC}"
    echo ""
fi

# Was replacing the image actually the effect? A deploy that changes nothing looks
# identical to one that works, until a rule goes missing in production.
NEW_VERSION=$(kubectl run "trd-version-$RANDOM" -n "$NAMESPACE" --image="$IMAGE_FULL" \
    --image-pull-policy=Never --rm -i --restart=Never --command -- trd version 2>/dev/null \
    | tr -d '\r' | head -1 || true)
echo -e "${BLUE}Image version:${NC} ${RUNNING_VERSION:-none} → ${NEW_VERSION:-unknown}"
if [[ -n "$RUNNING_VERSION" && "$RUNNING_VERSION" == "$NEW_VERSION" ]]; then
    echo -e "${YELLOW}   unchanged — the pods will run the same code as before.${NC}"
fi
echo ""

# One manifest, rewritten per deployment:
#   - hostPath   -> this machine's engine home (the committed value is a default)
#   - name       -> ${ENGINE_NAME}-scan, so two engines are two CronJobs
#   - component  -> ${ENGINE_NAME}, so `kubectl logs -l component=trd-day` picks
#                   out one engine. `app: trd` stays on both, keeping the
#                   existing `-l app=trd` recipe working across all of them.
#   - TRD_ENGINE_LABEL -> ${ENGINE_LABEL}, so pushed fills and chat replies name
#                   their sender with the word the bot and the report use.
#   - TRD_SCAN_NOTIFY  -> ${SCAN_NOTIFY}; the line after the name is the value.
render_manifest() {
    sed -E \
        -e "s#path: /Users/[^[:space:]]+#path: ${ENGINE_HOME}#" \
        -e "s#name: trd-engine-scan#name: ${ENGINE_NAME}-scan#" \
        -e "s#component: engine#component: ${ENGINE_NAME}#" \
        -e "s#value: trd-engine\$#value: ${ENGINE_LABEL}#" \
        -e "/name: TRD_SCAN_NOTIFY\$/{n;s#value: .*#value: \"${SCAN_NOTIFY}\"#;}" \
        k3s/trd-engine/cronjob.yaml
}

# The report, review and bot mount the live home only once it exists. Their
# manifests carry it on lines ending `# live-only` plus `,live=/engines/live` in
# the engine list; without ~/.trd-live those are deleted, because a `type:
# Directory` mount on a missing path fails the pod — the report and the bot
# would go down for an engine nobody has created yet.
LIVE_HOME="${LIVE_HOME:-$HOME/.trd-live}"
with_live_home() {
    if [[ -d "$LIVE_HOME" ]]; then
        sed -E \
            -e "s|path: /Users/[^[:space:]]*\.trd-live  # live-only\$|path: ${LIVE_HOME}|" \
            -e 's|  # live-only$||'
    else
        sed -E -e '/# live-only$/d' -e 's|,live=/engines/live||'
    fi
}
live_note() {
    [[ -d "$LIVE_HOME" ]] && echo ", live=${LIVE_HOME}" || echo " (no live home yet)"
}

# --- the post-market report --------------------------------------------------
# One CronJob covering EVERY engine, because the whole point is one message a day
# rather than one per engine per fill. It mounts the same homes the bot does
# and under the same names, so the engine called "day" in chat is the engine
# called "day" in the report.
#
# Not per-engine, so it is deployed once and does not take --day.
if [[ "$REPORT_MODE" == true ]]; then
    SWING_HOME="${SWING_HOME:-$HOME/.trd-engine}"
    DAY_HOME="${DAY_HOME:-$HOME/.trd-day}"
    SWING20_HOME="${SWING20_HOME:-$HOME/.trd-swing20}"

    for home in "$SWING_HOME" "$DAY_HOME" "$SWING20_HOME"; do
        if [[ ! -d "$home" ]]; then
            echo -e "${RED}✗ ${home} does not exist.${NC} Seed the engine before mounting it."
            exit 1
        fi
    done
    # Not fatal, unlike the bot: a report with no token still runs and prints to
    # the pod log, which is a legitimate way to run it while trying it out.
    if ! kubectl get secret trd-engine-telegram -n "$NAMESPACE" &>/dev/null; then
        echo -e "${YELLOW}⚠ No trd-engine-telegram secret — the report will log, not send.${NC}"
    fi

    echo -e "${YELLOW}⚙️  Applying the report CronJob...${NC}"
    kubectl apply -f k3s/trd-engine/namespace.yaml
    # Same per-suffix rewrite as the bot, for the same reason: `name: swing-home`
    # appears twice, so a sed range anchored on the volume name puts both
    # hostPaths on one engine. Matching the path itself has no such ambiguity.
    sed -E \
        -e "s#path: /Users/[^[:space:]]*\.trd-engine\$#path: ${SWING_HOME}#" \
        -e "s#path: /Users/[^[:space:]]*\.trd-day\$#path: ${DAY_HOME}#" \
        -e "s#path: /Users/[^[:space:]]*\.trd-swing20\$#path: ${SWING20_HOME}#" \
        k3s/trd-engine/daily-report-cronjob.yaml | with_live_home | kubectl apply -f -
    echo -e "${GREEN}✅ Applied${NC} (trd-engine-report, 16:16 ET weekdays → swing=${SWING_HOME}, day=${DAY_HOME}, swing20=${SWING20_HOME}$(live_note))"
    echo ""
    echo -e "${BLUE}Send one now (ignores the schedule, not the market calendar):${NC}"
    echo "    kubectl create job -n trd --from=cronjob/trd-engine-report report-now"
    echo "    kubectl logs -n trd -l component=report --tail=50"
    exit 0
fi

# --- the nightly decision review ---------------------------------------------
# One CronJob covering EVERY engine, like the report and for the same reason:
# one message a night. It runs `trd engine review --ai --snapshot --notify` at
# 16:31 ET — the arithmetic, then the model's read, stored per engine and sent.
# The model needs ANTHROPIC_API_KEY in a secret named trd-engine-ai; without it
# the deterministic review still runs, snapshots and sends.
if [[ "$REVIEW_MODE" == true ]]; then
    SWING_HOME="${SWING_HOME:-$HOME/.trd-engine}"
    DAY_HOME="${DAY_HOME:-$HOME/.trd-day}"
    SWING20_HOME="${SWING20_HOME:-$HOME/.trd-swing20}"

    for home in "$SWING_HOME" "$DAY_HOME" "$SWING20_HOME"; do
        if [[ ! -d "$home" ]]; then
            echo -e "${RED}✗ ${home} does not exist.${NC} Seed the engine before mounting it."
            exit 1
        fi
    done
    if ! kubectl get secret trd-engine-telegram -n "$NAMESPACE" &>/dev/null; then
        echo -e "${YELLOW}⚠ No trd-engine-telegram secret — the review will log, not send.${NC}"
    fi
    if ! kubectl get secret trd-engine-ai -n "$NAMESPACE" &>/dev/null; then
        echo -e "${YELLOW}⚠ No trd-engine-ai secret — the model's read is skipped; the arithmetic still runs.${NC}"
        echo "  Create it from the agents vault, never from a pasted value:"
        echo "    kubectl create secret generic trd-engine-ai -n trd \\"
        echo "      --from-literal=ANTHROPIC_API_KEY=\"\$(op read 'op://agents/anthropic_key/password')\""
    fi

    echo -e "${YELLOW}⚙️  Applying the review CronJob...${NC}"
    kubectl apply -f k3s/trd-engine/namespace.yaml
    sed -E \
        -e "s#path: /Users/[^[:space:]]*\.trd-engine\$#path: ${SWING_HOME}#" \
        -e "s#path: /Users/[^[:space:]]*\.trd-day\$#path: ${DAY_HOME}#" \
        -e "s#path: /Users/[^[:space:]]*\.trd-swing20\$#path: ${SWING20_HOME}#" \
        k3s/trd-engine/review-cronjob.yaml | with_live_home | kubectl apply -f -
    echo -e "${GREEN}✅ Applied${NC} (trd-engine-review, 16:31 ET weekdays → swing=${SWING_HOME}, day=${DAY_HOME}, swing20=${SWING20_HOME}$(live_note))"
    echo ""
    echo -e "${BLUE}Run one now (ignores the schedule, not the market calendar):${NC}"
    echo "    kubectl create job -n trd --from=cronjob/trd-engine-review review-now"
    echo "    kubectl logs -n trd -l component=review --tail=80 -f"
    exit 0
fi

# --- the Telegram command bot ------------------------------------------------
# A Deployment, not a CronJob: long polling has to stay resident. It shares the
# image and the engines' volumes but never opens a database — DuckDB is
# single-writer and the bot is up while a scan is not, so a connection held here
# would lock out the trading path. It reads the snapshots a scan publishes and
# writes command files the scan drains.
if [[ "$BOT_MODE" == true ]]; then
    SWING_HOME="${SWING_HOME:-$HOME/.trd-engine}"
    DAY_HOME="${DAY_HOME:-$HOME/.trd-day}"
    SWING20_HOME="${SWING20_HOME:-$HOME/.trd-swing20}"

    # Both refusals below are the same idea: fail with the fix rather than start
    # a pod that crash-loops with the answer buried in its logs.
    if ! kubectl get secret trd-engine-telegram -n "$NAMESPACE" &>/dev/null; then
        echo -e "${RED}✗ No trd-engine-telegram secret.${NC} The bot cannot start without a token."
        exit 1
    fi
    if ! kubectl get secret trd-engine-telegram -n "$NAMESPACE" \
        -o jsonpath='{.data.TRD_BOT_ALLOWED_USER_IDS}' 2>/dev/null | grep -q .; then
        echo -e "${RED}✗ The secret has no TRD_BOT_ALLOWED_USER_IDS.${NC}"
        echo "  The bot refuses to start without an allowlist — an unrestricted bot takes"
        echo "  commands from anyone who finds it. Add your NUMERIC Telegram user id:"
        echo ""
        echo "    kubectl patch secret trd-engine-telegram -n trd \\"
        echo "      -p '{\"stringData\":{\"TRD_BOT_ALLOWED_USER_IDS\":\"123456789\"}}'"
        echo ""
        echo "  Usernames are not accepted: they are changeable and re-registerable."
        exit 1
    fi
    for home in "$SWING_HOME" "$DAY_HOME" "$SWING20_HOME"; do
        if [[ ! -d "$home" ]]; then
            echo -e "${RED}✗ ${home} does not exist.${NC} Seed the engine before mounting it."
            exit 1
        fi
    done

    echo -e "${YELLOW}⚙️  Applying the bot Deployment...${NC}"
    kubectl apply -f k3s/trd-engine/namespace.yaml
    # Rewrite each hostPath by the suffix it already carries, NOT by a sed range
    # anchored on the volume name.
    #
    # The range version shipped broken and is worth remembering: `name:
    # swing-home` appears twice in the manifest (volumeMounts and volumes), so
    # `/name: day-home/,/type: DirectoryOrCreate/` opened at the day *mount* and
    # closed at the swing *volume* — putting the swing hostPath inside the day
    # expression, where it was rewritten second and won. Both volumes ended up on
    # ~/.trd-day, the swing home was not mounted at all, and both engines' queue
    # files landed in one directory under the same update-id filename, so one
    # silently overwrote the other.
    #
    # Matching the path itself has no such ambiguity: whatever the committed
    # default is, `.trd-engine` is the swing home, `.trd-day` the day home and
    # `.trd-swing20` the swing20 home. Each is anchored at end of line so no
    # suffix can match inside another engine's path.
    sed -E \
        -e "s#path: /Users/[^[:space:]]*\.trd-engine\$#path: ${SWING_HOME}#" \
        -e "s#path: /Users/[^[:space:]]*\.trd-day\$#path: ${DAY_HOME}#" \
        -e "s#path: /Users/[^[:space:]]*\.trd-swing20\$#path: ${SWING20_HOME}#" \
        k3s/trd-engine/bot-deployment.yaml | with_live_home | kubectl apply -f -
    echo -e "${GREEN}✅ Applied${NC} (trd-engine-bot → swing=${SWING_HOME}, day=${DAY_HOME}, swing20=${SWING20_HOME}$(live_note))"
    echo ""

    # One poller, or Telegram permanently 409s the second one. Worth asserting
    # rather than assuming: a stuck terminating pod is exactly how two end up
    # running at once.
    kubectl rollout status deployment/trd-engine-bot -n "$NAMESPACE" --timeout=90s || true
    RUNNING=$(kubectl get pods -n "$NAMESPACE" -l component=bot \
        --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')
    echo -e "${BLUE}Bot pods running:${NC} ${RUNNING}"
    if [[ "$RUNNING" -gt 1 ]]; then
        echo -e "${RED}⚠️  More than one poller. Telegram allows a single getUpdates per token${NC}"
        echo -e "${RED}   and answers the rest with a permanent 409.${NC}"
    fi
    echo ""
    echo -e "${BLUE}Useful:${NC}"
    echo "    kubectl logs -n trd -l component=bot -f"
    echo "    kubectl exec -n trd deploy/trd-engine-bot -- trd bot check"
    exit 0
fi

echo -e "${YELLOW}⚙️  Applying manifests...${NC}"
kubectl apply -f k3s/trd-engine/namespace.yaml
render_manifest | kubectl apply -f -
echo -e "${GREEN}✅ Applied${NC} (${ENGINE_NAME}-scan, hostPath → ${ENGINE_HOME})"

# The queue drain, deployed with its engine because it is per-home: it applies
# the commands typed in chat when the scan is not running, which is every hour
# the market is closed. Same rewrite, same reasons.
sed -E \
    -e "s#path: /Users/[^[:space:]]+#path: ${ENGINE_HOME}#" \
    -e "s#name: trd-engine-queue#name: ${ENGINE_NAME}-queue#" \
    -e "s#value: trd-engine\$#value: ${ENGINE_LABEL}#" \
    k3s/trd-engine/apply-queue-cronjob.yaml | kubectl apply -f -
echo -e "${GREEN}✅ Applied${NC} (${ENGINE_NAME}-queue, every 10 min outside the session)"

# The live engine's own post-market message, position by position (SB-1406).
# Per-home like the queue drain, so it ships with --live and mounts that home only.
if [[ "$LIVE_MODE" == true ]]; then
    sed -E -e "s#path: /Users/[^[:space:]]+#path: ${ENGINE_HOME}#" \
        k3s/trd-engine/live-report-cronjob.yaml | kubectl apply -f -
    echo -e "${GREEN}✅ Applied${NC} (trd-live-report, 16:20 ET weekdays → ${ENGINE_HOME}, private alert chat)"
fi
echo ""

if ! kubectl get secret trd-engine-telegram -n "$NAMESPACE" &>/dev/null; then
    echo -e "${YELLOW}⚠️  No Telegram secret — fills will not be pushed.${NC}"
    echo -e "   See k3s/trd-engine/secret.example.yaml for the create command."
    echo ""
fi

echo -e "${BLUE}CronJob:${NC}"
kubectl get cronjob -n "$NAMESPACE"
echo ""

# A one-off pod running the same image against the same database, with the
# market-hours guard switched off.
#
# NOT `kubectl create job --from=cronjob` plus `kubectl set env`: a Job's pod
# template is immutable once created, so the env edit is rejected and the pod
# runs with the guard still on — it exits 0 having done nothing, which looks
# like success. Building the pod spec up front avoids that entirely.
run_once() {
    local name="$1"
    shift
    # printf with a format but zero args still runs the format once, so a bare
    # `printf '"%s",' "$@"` yields `""` — the pod then gets one empty-string arg
    # and the entrypoint hands it to trd, which dies on `No such command ''`.
    local args_json=""
    if [[ $# -gt 0 ]]; then
        args_json=$(printf '"%s",' "$@" | sed 's/,$//')
    fi
    kubectl run "$name" -n "$NAMESPACE" --rm -i --restart=Never \
        --image="$IMAGE_FULL" \
        --overrides="$(cat <<JSON
{
  "spec": {
    "restartPolicy": "Never",
    "containers": [{
      "name": "trd",
      "image": "${IMAGE_FULL}",
      "imagePullPolicy": "Never",
      "args": [${args_json}],
      "env": [
        {"name": "TRD_HOME", "value": "/data"},
        {"name": "TZ", "value": "America/New_York"},
        {"name": "NO_COLOR", "value": "1"},
        {"name": "TRD_ENGINE_FORCE", "value": "1"},
        {"name": "TRD_ENGINE_LABEL", "value": "${ENGINE_LABEL}"},
        {"name": "TRD_SCAN_NOTIFY", "value": "${SCAN_NOTIFY}"}
      ],
      "envFrom": [{"secretRef": {"name": "trd-engine-telegram", "optional": true}}],
      "volumeMounts": [{"name": "trd-home", "mountPath": "/data"}]
    }],
    "volumes": [{
      "name": "trd-home",
      "hostPath": {"path": "${ENGINE_HOME}", "type": "Directory"}
    }]
  }
}
JSON
)"
}

if [[ "$RUN_TEST" == true ]]; then
    echo -e "${YELLOW}🚀 Running one scan now (market-hours guard bypassed)...${NC}"
    # No args -> the entrypoint's normal path: guard, daily sync, scan, publish.
    run_once "${ENGINE_NAME}-test-$(date +%s)" || true
    echo ""
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ Deployment Complete${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${BLUE}Useful commands:${NC}"
echo "  Watch this engine's scans live (drop the selector for all engines):"
echo "    kubectl logs -n $NAMESPACE -l component=${ENGINE_NAME} --tail=100 -f"
echo ""
echo "  Run one scan right now:"
echo "    ./scripts/deploy-k3s.sh --skip-build --test${MODE_FLAG}"
echo ""
echo "  Read the scorecard — easiest from the host, same database:"
echo "    TRD_HOME=${ENGINE_HOME} trd engine report"
echo "    TRD_HOME=${ENGINE_HOME} trd engine positions"
echo ""
