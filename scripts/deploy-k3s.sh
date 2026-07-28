#!/bin/bash
#
# Deploy the trd engine to k3s.
#
# Usage:
#   ./scripts/deploy-k3s.sh                 # build image, import, apply manifests
#   ./scripts/deploy-k3s.sh --skip-build    # apply manifests against the existing image
#   ./scripts/deploy-k3s.sh --test          # apply, then run one scan now (ignores market hours)
#
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
while [[ $# -gt 0 ]]; do
    case $1 in
        --skip-build) SKIP_BUILD=true; shift ;;
        --test) RUN_TEST=true; shift ;;
        *) echo "Unknown option: $1"; echo "Usage: $0 [--skip-build] [--test]"; exit 1 ;;
    esac
done

# The engine's own database. Deliberately NOT your real trd database: this one
# holds a paper account and ten tickers' price history, nothing else.
ENGINE_HOME="${ENGINE_HOME:-$HOME/.trd-engine}"

CURRENT_CONTEXT=$(kubectl config current-context)
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}K3s Deployment: trd engine${NC}"
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

if ! TRD_HOME="$ENGINE_HOME" trd engine rules &> /dev/null \
   || ! TRD_HOME="$ENGINE_HOME" trd engine positions &> /dev/null; then
    echo -e "${YELLOW}🌱 Seeding the engine database at ${ENGINE_HOME}...${NC}"
    echo -e "${BLUE}   (a separate paper database — your real trd data is elsewhere)${NC}"
    TRD_HOME="$ENGINE_HOME" trd init
    TRD_HOME="$ENGINE_HOME" trd engine init
    echo -e "${YELLOW}   Downloading 2 years of daily bars (the rules need 200)...${NC}"
    TRD_HOME="$ENGINE_HOME" trd sync --full
    echo -e "${GREEN}✅ Engine database ready${NC}"
else
    echo -e "${GREEN}✅ Engine database already seeded${NC}"
fi
echo ""

if [[ "$SKIP_BUILD" == false ]]; then
    echo -e "${YELLOW}📦 Building image...${NC}"
    docker build -t "$IMAGE_FULL" .
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
fi

echo -e "${YELLOW}⚙️  Applying manifests...${NC}"
kubectl apply -f k3s/trd-engine/namespace.yaml
# Rewrite the hostPath to this machine's engine home, so the manifest does not
# have to be hand-edited per user. The committed value is only a default.
sed -E "s#path: /Users/[^[:space:]]+#path: ${ENGINE_HOME}#" k3s/trd-engine/cronjob.yaml \
    | kubectl apply -f -
echo -e "${GREEN}✅ Applied${NC} (hostPath → ${ENGINE_HOME})"
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
    run_once "trd-engine-test-$(date +%s)" || true
    echo ""
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}✅ Deployment Complete${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo -e "${BLUE}Useful commands:${NC}"
echo "  Watch scans live:"
echo "    kubectl logs -n $NAMESPACE -l app=trd --tail=100 -f"
echo ""
echo "  Run one scan right now:"
echo "    ./scripts/deploy-k3s.sh --skip-build --test"
echo ""
echo "  Read the scorecard — easiest from the host, same database:"
echo "    TRD_HOME=${ENGINE_HOME} trd engine report"
echo "    TRD_HOME=${ENGINE_HOME} trd engine positions"
echo ""
