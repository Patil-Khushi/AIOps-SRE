#!/usr/bin/env bash
# infra/teardown.sh — uninstall the OTel demo from Rancher Desktop k3s.
# Leaves the cluster itself running (we don't own it).
set -euo pipefail

NAMESPACE="${NAMESPACE:-otel-demo}"
KEEP_NAMESPACE="${KEEP_NAMESPACE:-0}"

if helm list -n "$NAMESPACE" -q 2>/dev/null | grep -qx 'otel-demo'; then
  echo "==> Uninstalling helm release 'otel-demo' from namespace '$NAMESPACE'"
  helm uninstall otel-demo -n "$NAMESPACE"
else
  echo "    no helm release 'otel-demo' to uninstall"
fi

if [ "$KEEP_NAMESPACE" != "1" ]; then
  if kubectl get ns "$NAMESPACE" -o name >/dev/null 2>&1; then
    echo "==> Deleting namespace '$NAMESPACE'"
    kubectl delete ns "$NAMESPACE" --wait=false
  fi
fi

echo "Done."
