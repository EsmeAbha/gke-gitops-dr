#!/usr/bin/env bash
# The full DR drill on a kind cluster: two namespaces stand in for the two regions, an
# S3-compatible store stands in for the multi-region bucket. Everything else (operator, plugin,
# Cluster specs) is exactly what Argo CD deploys to GKE.
set -euo pipefail
cd "$(dirname "$0")/.."
OUT=${OUT:-drill-results}

group() { echo "::group::$*"; }
endgroup() { echo "::endgroup::"; }

wait_healthy() {   # namespace cluster
  for _ in $(seq 1 120); do
    [ "$(kubectl -n "$1" get clusters.postgresql.cnpg.io "$2" -o jsonpath='{.status.phase}' 2>/dev/null)" = "Cluster in healthy state" ] && return 0
    sleep 5
  done
  kubectl -n "$1" describe clusters.postgresql.cnpg.io "$2"; kubectl -n "$1" get pods; return 1
}

wait_backup() {   # namespace
  for _ in $(seq 1 120); do
    kubectl -n "$1" get backups.postgresql.cnpg.io -o jsonpath='{.items[*].status.phase}' | grep -q completed && return 0
    sleep 5
  done
  kubectl -n "$1" get backups.postgresql.cnpg.io -o yaml; return 1
}

group "Platform: the kustomizations Argo CD syncs"
kubectl apply --server-side -k platform/cert-manager
kubectl -n cert-manager wait --for=condition=Available deployment --all --timeout=300s
kubectl apply --server-side -k platform/cloudnative-pg
kubectl -n cnpg-system wait --for=condition=Available deployment/cnpg-controller-manager --timeout=300s
kubectl apply --server-side -k platform/barman-cloud
kubectl -n cnpg-system rollout status deployment/barman-cloud --timeout=300s
endgroup

group "Object store"
kubectl apply -f ci/object-store.yaml
kubectl -n object-store rollout status deployment/object-store --timeout=300s
kubectl -n object-store port-forward svc/object-store 9000:9000 >/dev/null & PF=$!
sleep 3
AWS_ACCESS_KEY_ID=rustfsadmin AWS_SECRET_ACCESS_KEY=rustfsadmin AWS_DEFAULT_REGION=us-east-1 \
  aws --endpoint-url http://127.0.0.1:9000 s3 mb s3://backups
kill $PF
for ns in db-sg db-tw; do
  kubectl create namespace "$ns"
  kubectl -n "$ns" create secret generic s3-creds \
    --from-literal=ACCESS_KEY_ID=rustfsadmin --from-literal=ACCESS_SECRET_KEY=rustfsadmin
done
endgroup

group "Region sg: primary, first base backup"
kubectl apply -k deploy/kind-sg
wait_healthy db-sg app-db-sg
wait_backup db-sg
endgroup

group "Region tw: replica cluster bootstrapped from sg's backup, replaying its WAL"
kubectl apply -k deploy/kind-tw
wait_healthy db-tw app-db-tw
endgroup

rc=0
group "Drill 1: point-in-time restore after a destructive change"
python -m drill pitr --cluster app-db-sg@db-sg --rpo 0 --rto 600 --environment kind --out "$OUT" || rc=$?
endgroup
group "Drill 2: lose region sg, fail over to tw"
python -m drill region-loss --primary app-db-sg@db-sg --standby app-db-tw@db-tw --confirm-destroy app-db-sg \
  --rpo 30 --rto 300 --warmup 60 --environment kind --out "$OUT" || rc=$((rc > 0 ? rc : $?))
endgroup
exit $rc
