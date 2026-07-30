#!/bin/bash
#
# Deploy the trd engine to k3s.
#
# Usage:
#   ./scripts/deploy-k3s.sh                 # build image, import, apply manifests
#   ./scripts/deploy-k3s.sh --skip-build    # apply manifests against the existing image
#   ./scripts/deploy-k3s.sh --test          # apply, then run one scan now (ignores market hours)
#   ./scripts/deploy-k3s.sh --day           # the day-mode engine (~/.trd-day) instead
#
# Two engines can run side by side: they share the image, the namespace and the
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
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-build) SKIP_BUILD=true; shift ;;
        --test) RUN_TEST=true; shift ;;
        --day) DAY_MODE=true; shift ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--skip-build] [--test] [--day]"
            exit 1
            ;;
    esac
done

# Which engine this invocation deploys. The name is load-bearing: it becomes the
# CronJob name, so without it a second deployment would REPLACE the first and
# silently repoint the running engine at the other database.
if [[ "$DAY_MODE" == true ]]; then
    ENGINE_NAME="${ENGINE_NAME:-trd-day}"
    ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-day}"
    DAY_FLAG=" --day"
    # Load-bearing: an engine seeded without this is a swing engine wearing a day
    # engine's name — it would carry positions overnight, the one thing day mode
    # exists to prevent.
    INIT_FLAGS=(--flat-at "${FLAT_AT:-1555}")
else
    ENGINE_NAME="${ENGINE_NAME:-trd-engine}"
    # The engine's own database. Deliberately NOT your real trd database: this
    # one holds a paper account and its universe's price history, nothing else.
    ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-engine}"
    DAY_FLAG=""
    INIT_FLAGS=()
fi

CURRENT_CONTEXT=$(kubectl config current-context)
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}K3s Deployment: ${ENGINE_NAME}${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "Context:     ${YELLOW}${CURRENT_CONTEXT}${NC}"
echo -e "Engine DB:   ${YELLOW}${ENGINE_HOME}${NC}  (paper only)"
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

if needs_seed; then
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
#   - TRD_ENGINE_LABEL -> ${ENGINE_NAME}, so pushed fills name their sender.
render_manifest() {
    sed -E \
        -e "s#path: /Users/[^[:space:]]+#path: ${ENGINE_HOME}#" \
        -e "s#name: trd-engine-scan#name: ${ENGINE_NAME}-scan#" \
        -e "s#component: engine#component: ${ENGINE_NAME}#" \
        -e "s#value: trd-engine\$#value: ${ENGINE_NAME}#" \
        k3s/trd-engine/cronjob.yaml
}

echo -e "${YELLOW}⚙️  Applying manifests...${NC}"
kubectl apply -f k3s/trd-engine/namespace.yaml
render_manifest | kubectl apply -f -
echo -e "${GREEN}✅ Applied${NC} (${ENGINE_NAME}-scan, hostPath → ${ENGINE_HOME})"
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
        {"name": "TRD_ENGINE_FORCE", "value": "1"}
      ],
      "envFrom": [{"secretRef": {"name": "trd-engine-telegram", "optional": true}}],
      "volumeMounts": [{"name": "trd-home", "mountPath": "/data"}]
    }],
    "volumes": [{
      "name": "trd-home",
      "hostPath": {"path": "${ENGINE_HOME}", "type": "DirectoryOrCreate"}
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
echo "    ./scripts/deploy-k3s.sh --skip-build --test${DAY_FLAG}"
echo ""
echo "  Read the scorecard — easiest from the host, same database:"
echo "    TRD_HOME=${ENGINE_HOME} trd engine report"
echo "    TRD_HOME=${ENGINE_HOME} trd engine positions"
echo ""
