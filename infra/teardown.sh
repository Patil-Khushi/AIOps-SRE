#!/usr/bin/env bash
# infra/teardown.sh — uninstall the OTel demo. Leaves Rancher Desktop / k3s alone.
set -euo pipefail

NAMESPACE="${NAMESPACE:-otel-demo}"
KEEP_NAMESPACE="${KEEP_NAMESPACE:-0}"

command -v helm >/dev/null 2>&1 || { echo "helm not on PATH." >&2; exit 1; }

if helm -n "$NAMESPACE" ls -q 2>/dev/null | grep -qx 'otel-demo'; then
  echo "Uninstalling otel-demo Helm release..."
  helm -n "$NAMESPACE" uninstall otel-demo
else
  echo "No otel-demo release in namespace '$NAMESPACE' to uninstall."
fi

if [ "$KEEP_NAMESPACE" != "1" ]; then
  echo "Deleting namespace '$NAMESPACE'..."
  kubectl delete namespace "$NAMESPACE" --ignore-not-found=true
fi

echo
echo "Done. Rancher Desktop / k3s itself is still running — stop it from"
echo "the Rancher Desktop UI if you want to free the RAM."
