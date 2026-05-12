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

# Skip install if otel-demo is already healthy — running helm upgrade after
# inject.py has patched flagd-config triggers a server-side-apply conflict.
# Pass FORCE=1 to override (clear flagd patches first or run teardown.sh).
already_healthy=0
if [ "${FORCE:-0}" != "1" ]; then
  existing=$(helm -n "$NAMESPACE" ls --filter '^otel-demo$' -o json 2>/dev/null)
  if [ -n "$existing" ] && [ "$existing" != "[]" ]; then
    status=$(echo "$existing" | python3 -c 'import json,sys;print(json.load(sys.stdin)[0].get("status",""))' 2>/dev/null || echo '')
    if [ "$status" = "deployed" ]; then
      ready=$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/component=frontend-proxy \
        -o "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}" 2>/dev/null || echo '')
      [ "$ready" = "True" ] && already_healthy=1
    fi
  fi
fi

if [ "$already_healthy" = "1" ]; then
  echo "==> otel-demo already deployed and frontend-proxy is Ready; skipping helm install."
  echo "    To force a re-install: FORCE=1 ./infra/bootstrap.sh"
  echo "    Or wipe and reinstall: ./infra/teardown.sh && ./infra/bootstrap.sh"
else
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
