#!/usr/bin/env bash
# Put your GCP project id into the GKE storage component (bucket name and backup identity),
# then commit the result: Argo CD deploys what's in Git.   usage: scripts/set-project.sh my-project
set -euo pipefail
[ $# -eq 1 ] || { echo "usage: $0 PROJECT_ID" >&2; exit 2; }
cd "$(dirname "$0")/.."
sed -i "s/PROJECT_ID/$1/g" database/components/gcs/kustomization.yaml database/components/gcs/objectstore.yaml
grep -n "$1" database/components/gcs/*.yaml
