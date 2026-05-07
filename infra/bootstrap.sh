#!/usr/bin/env bash
# infra/bootstrap.sh — POSIX equivalent of bootstrap.ps1.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-aiops-poc}"
NAMESPACE="${NAMESPACE:-otel-demo}"
CHART_VERSION="${CHART_VERSION:-}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "Required tool '$1' not on PATH." >&2; exit 1; }
}

echo "==> Checking prerequisites"
require docker
require kind
require kubectl
require helm

echo "==> Verifying Docker is running"
docker info --format '{{.ServerVersion}}' >/dev/null

echo "==> Ensuring kind cluster '$CLUSTER_NAME'"
if kind get clusters | grep -qx "$CLUSTER_NAME"; then
  echo "    cluster already exists; skipping create"
else
  kind create cluster --name "$CLUSTER_NAME" --config "$repo_root/infra/kind-config.yaml"
fi

kubectl cluster-info --context "kind-$CLUSTER_NAME" >/dev/null

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

set_nodeport() {
  local svc="$1" node_port="$2" target_port="$3"
  echo "==> Exposing $svc as NodePort $node_port"
  kubectl -n "$NAMESPACE" patch service "$svc" --type='json' -p "[
    {\"op\":\"replace\",\"path\":\"/spec/type\",\"value\":\"NodePort\"},
    {\"op\":\"replace\",\"path\":\"/spec/ports/0/nodePort\",\"value\":$node_port},
    {\"op\":\"replace\",\"path\":\"/spec/ports/0/targetPort\",\"value\":$target_port}
  ]" 2>/dev/null || true
}

set_nodeport otel-demo-frontendproxy 30080 8080
set_nodeport otel-demo-grafana       30300 3000
set_nodeport otel-demo-jaeger-query  30168 16686

cat <<EOF

==> Done.
    Frontend:  http://localhost:8080
    Grafana:   http://localhost:3000   (admin / admin by default)
    Jaeger:    http://localhost:16686

Trigger a failure:
    uv run python -m demo.failure_injection.inject --list
    uv run python -m demo.failure_injection.inject slow-product-catalog

Tear down:  ./infra/teardown.sh
EOF
