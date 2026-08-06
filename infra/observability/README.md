# `infra/observability/` — the standalone observability stack

Prometheus + Alertmanager, Grafana, Jaeger and the OpenTelemetry Collector,
installed as **four independent Helm releases** into the `observability`
namespace.

## Why this replaced the OTel Demo's bundled stack

The `opentelemetry-demo` umbrella chart shipped Prometheus, Grafana, Jaeger and
the Collector as **subcharts**. That single fact is why the demo could not
simply be deleted: `helm uninstall otel-demo` would have taken the entire
observability stack with it, leaving the ecommerce SUT unmonitored.

Splitting them out first made the uninstall a non-event.

## Install

```powershell
.\infra\observability\install.ps1
```

Idempotent (`helm upgrade --install` throughout). Order matters: Jaeger goes in
before the Collector, whose traces pipeline exports to it.

## What is deliberately NOT here

**Loki.** It was always its own Helm release, and it stays in the `otel-demo`
namespace. Reinstalling it would drop its PVC and every log line collected so
far. Grafana's Loki datasource points at
`loki.otel-demo.svc.cluster.local:3100` for exactly this reason. Moving it is a
clean-up task with a real cost and no functional benefit.

**A logs pipeline in the Collector.** The demo's collector had an `otlp`
receiver and no `filelog`, so pod stdout never reached it — which is why
`demo/ecommerce/k8s/40-promtail.yaml` exists. Promtail tails the pods and
writes to Loki directly. Routing logs through the Collector would add a hop and
a failure mode for nothing.

## Two behaviour changes to know about

**1. Jaeger serves its API at the root.** The OTel Demo's Jaeger v2 config used
`base_path: /jaeger/ui`, so the query API lived at `/jaeger/ui/api/services`.
This deployment does not. Set:

```
AIOPS_JAEGER_API_PREFIX=
```

Leave the old `/jaeger/ui` value in place and every trace lookup 404s — which
reads as "no traces" rather than as a misconfiguration.

**2. Alertmanager now exists.** Rules previously evaluated with nowhere to go;
agents polled `/api/v1/alerts`. Prometheus is now wired to Alertmanager, so
routing, grouping and silencing are available.

## Endpoints

```powershell
kubectl -n observability port-forward svc/prometheus-server 9090:80
kubectl -n observability port-forward svc/grafana 3001:80
kubectl -n observability port-forward svc/jaeger 16686:16686
kubectl -n observability port-forward svc/prometheus-alertmanager 9093:9093
```

Corresponding `.env`:

```
AIOPS_PROMETHEUS_URL=http://localhost:9090
AIOPS_JAEGER_URL=http://localhost:16686
AIOPS_JAEGER_API_PREFIX=
AIOPS_GRAFANA_URL=http://localhost:3001
AIOPS_LOKI_URL=http://localhost:3100
```

Grafana is `admin` / `admin` and is served under `/grafana/`.

## Gotchas

- **`jaeger` is one Service, not three.** allInOne exposes every port on a
  single Service named `jaeger`; there is no `jaeger-collector` or
  `jaeger-query` in this mode. Re-check after any chart bump.
- **Datasource UIDs are load-bearing.** `webstore-metrics` and
  `webstore-traces` are kept from the demo chart because saved dashboards and
  the RA-003 panel-render path reference datasources by uid. Renaming them
  silently breaks every existing panel link.
- **Traces are in-memory.** allInOne loses spans on restart. Fine for a POC;
  swap the storage backend before anyone relies on trace history.
- **`grafana/grafana` reports itself as deprecated** on install. It still works;
  note it as a future migration.
