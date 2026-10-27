#!/usr/bin/env bash
# One-time setup of a region: install Argo CD and hand it the root Application. After this,
# every change (including a failover) goes through Git.   usage: scripts/bootstrap.sh gke-sg|gke-tw
set -euo pipefail
CLUSTER=${1:?usage: $0 gke-sg|gke-tw}
ARGOCD_VERSION=v3.5.6
cd "$(dirname "$0")/.."

kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
# Argo CD does not assess the health of child Applications by default, so sync waves between
# them would only order the start of each sync, not wait for operators to be ready.
kubectl -n argocd patch configmap argocd-cm --type merge -p "$(cat <<'PATCH'
{"data": {"resource.customizations.health.argoproj.io_Application": "hs = {}\nhs.status = \"Progressing\"\nhs.message = \"\"\nif obj.status ~= nil and obj.status.health ~= nil then\n  hs.status = obj.status.health.status\n  if obj.status.health.message ~= nil then hs.message = obj.status.health.message end\nend\nreturn hs\n"}}
PATCH
)"
kubectl -n argocd rollout status deployment/argocd-repo-server --timeout=300s
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=300s
kubectl apply -f "argocd/bootstrap/${CLUSTER}.yaml"
echo "Argo CD is reconciling argocd/clusters/${CLUSTER}. Watch: kubectl -n argocd get applications -w"
