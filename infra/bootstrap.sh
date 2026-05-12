#!/usr/bin/env bash
# infra/bootstrap.sh — POSIX equivalent of bootstrap.ps1.
# Targets Rancher Desktop (k3s) by default. Set CONTEXT='' to use the current kube context.
set -euo pipefail

NAMESPACE="${NAMESPACE:-otel-demo}"
CHART_VERSION="${CHART_VERSION:-}"
CONTEXT="${CONTEXT:-rancher-desktop}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "Required tool '$1' not on PATH." >&2; exit 1; }
}

echo "==> Checking prerequisites"
require kubectl
require helm

if [ -n "$CONTEXT" ]; then
  current="$(kubectl config current-context 2>/dev/null || true)"
  if [ "$current" != "$CONTEXT" ]; then
    echo "    switching kube context: $current -> $CONTEXT"
    kubectl config use-context "$CONTEXT" >/dev/null
  fi
fi

echo "==> Verifying Rancher Desktop k3s is reachable"
if ! kubectl version --request-timeout=5s >/dev/null 2>&1; then
  cat >&2 <<'EOF'
Cannot reach the Kubernetes API.

  Start Rancher Desktop and wait for the tray icon to show 'Kubernetes: running'
  (usually 30-60 seconds). Then re-run this script.

  If Rancher Desktop is already running, check `kubectl config current-context`
  matches 'rancher-desktop' and that k3s is enabled (Settings -> Kubernetes).
EOF
  exit 1
fi

echo "==> Adding OpenTelemetry Helm repo"
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

echo "==> Installing OTel demo into namespace '$NAMESPACE'"
helm_args=(
  upgrade --install otel-demo open-telemetry/opentelemetry-demo
  --namespace "$NAMESPACE" --create-namespace
  --values "$repo_root/demo/otel-demo/values.yaml"
  --wait --timeout 15m
)
[ -n "$CHART_VERSION" ] && helm_args+=(--version "$CHART_VERSION")
helm "${helm_args[@]}"

echo "==> Waiting for frontend pod to be Ready"
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod \
  -l app.kubernetes.io/component=frontend --timeout=300s

cat <<EOF

==> Done.

Open a separate shell and run:
    kubectl -n $NAMESPACE port-forward svc/frontend-proxy 8080:8080

Then browse:
    Frontend:  http://localhost:8080/
    Grafana:   http://localhost:8080/grafana/   (admin / admin by default)
    Jaeger:    http://localhost:8080/jaeger/ui/

Trigger a failure:
    uv run python -m demo.failure_injection.inject --list
    uv run python -m demo.failure_injection.inject slow-product-catalog

Tear down:  ./infra/teardown.sh
EOF
