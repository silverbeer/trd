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

CURRENT_CONTEXT=$(kubectl config current-context)
echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}K3s Deployment: trd engine${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "Context: ${YELLOW}${CURRENT_CONTEXT}${NC}"
echo ""

# The engine writes trades. Deploying it to the wrong cluster is not a no-op, so
# make the operator look at the context name before anything happens.
read -r -p "Deploy to this context? [y/N] " confirm
[[ "$confirm" == "y" || "$confirm" == "Y" ]] || { echo "Aborted."; exit 1; }
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
kubectl apply -f k3s/trd-engine/cronjob.yaml
echo -e "${GREEN}✅ Applied${NC}"
echo ""

if ! kubectl get secret trd-engine-telegram -n "$NAMESPACE" &>/dev/null; then
    echo -e "${YELLOW}⚠️  No Telegram secret — fills will not be pushed.${NC}"
    echo -e "   See k3s/trd-engine/secret.example.yaml for the create command."
    echo ""
fi

echo -e "${BLUE}CronJob:${NC}"
kubectl get cronjob -n "$NAMESPACE"
echo ""

if [[ "$RUN_TEST" == true ]]; then
    JOB="trd-engine-test-$(date +%s)"
    echo -e "${YELLOW}🚀 Running one scan now (market-hours guard bypassed)...${NC}"
    kubectl create job --from=cronjob/trd-engine-scan "$JOB" -n "$NAMESPACE"
    # The CronJob's pod template is copied verbatim, so force the guard off here.
    kubectl set env job/"$JOB" -n "$NAMESPACE" TRD_ENGINE_FORCE=1
    kubectl wait --for=condition=complete --timeout=300s job/"$JOB" -n "$NAMESPACE" || true
    echo ""
    kubectl logs -n "$NAMESPACE" job/"$JOB" --tail=50 || true
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
echo "    kubectl create job --from=cronjob/trd-engine-scan manual-\$(date +%s) -n $NAMESPACE"
echo ""
echo "  Read the scorecard (uses the same image):"
echo "    kubectl run trd-report -n $NAMESPACE --rm -it --restart=Never \\"
echo "      --image=$IMAGE_FULL --overrides='{\"spec\":{\"containers\":[{\"name\":\"trd-report\",\"image\":\"$IMAGE_FULL\",\"args\":[\"engine\",\"report\"],\"volumeMounts\":[{\"name\":\"d\",\"mountPath\":\"/data\"}]}],\"volumes\":[{\"name\":\"d\",\"hostPath\":{\"path\":\"/Users/tomdrake/.trd-engine\"}}]}}'"
echo ""
echo "  Or straight from the host, same database:"
echo "    TRD_HOME=~/.trd-engine trd engine report"
echo ""
