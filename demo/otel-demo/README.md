# OpenTelemetry Demo — Helm values

We use the upstream chart unchanged except for `values.yaml` here. The bootstrap script does:

```bash
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts
helm repo update
helm upgrade --install otel-demo open-telemetry/opentelemetry-demo \
  --namespace otel-demo --create-namespace \
  --values demo/otel-demo/values.yaml
```

After install, `infra/bootstrap.ps1` port-forwards the frontend (`8080`) and the bundled Grafana (`3000`).

## Failure flags we use

The chart bundles `flagd`. The Phase-0 scenarios in `demo/failure_injection/scenarios/` map to flags documented at <https://opentelemetry.io/docs/demo/feature-flags/>. We hit flagd's HTTP endpoint inside the cluster; that's why the failure-injection module port-forwards through `kubectl`.
