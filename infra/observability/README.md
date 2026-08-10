# `infra/observability/` — the standalone observability stack

Prometheus + Alertmanager, Grafana and Jaeger, installed as **three independent
Helm releases** into the `observability` namespace.

Every signal travels from the application straight to its backend — Prometheus
scrapes `/metrics`, spans go OTLP to Jaeger, log lines are pushed to Loki by the
app itself. There is no collector and no log shipper in any of the three paths.

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

Idempotent (`helm upgrade --install` throughout). It also uninstalls a leftover
`otel-collector` release if it finds one — see below.

## What is deliberately NOT here

**Loki.** It was always its own Helm release, and it stays in the `otel-demo`
namespace. Reinstalling it would drop its PVC and every log line collected so
far. Grafana's Loki datasource points at
`loki.otel-demo.svc.cluster.local:3100` for exactly this reason. Moving it is a
clean-up task with a real cost and no functional benefit.

**The OpenTelemetry Collector.** It used to sit between the app and Jaeger, but
its entire configuration was a single `otlp` receiver forwarding to
`jaeger:4317` — it parsed nothing, sampled nothing and enriched nothing. Jaeger's
allInOne mode accepts OTLP natively on 4317/4318, so the services now export
straight to it and the hop is gone, along with a 384Mi pod on a node that was
already ~92% committed.

`install.ps1` uninstalls the release if it is still present. Leaving it running
would be worse than useless: its Service stays resolvable, so any pod whose
ConfigMap had not been rolled yet keeps exporting there, and traces split
across two paths in a way that reads as random span loss.

**A log shipper.** Promtail used to run as a DaemonSet tailing `/var/log/pods`.
Each service now pushes its own JSON log lines to Loki's
`/loki/api/v1/push` from a background thread
(`demo/ecommerce/*/src/observability/loki_handler.py`), so logs reach the
backend the same way metrics and traces do. stdout logging is unchanged, so
`kubectl logs` still works.

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
kubectl -n otel-demo      port-forward svc/loki 3100:3100
```

The application reaches both write endpoints without any of the above — in-cluster
DNS from `demo/ecommerce/k8s/01-config.yaml`, or the NodePort bridge in
`demo/ecommerce/observability/nodeports.yaml` when it runs under Compose. The
port-forwards are only for you and for the agents running on the host.

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
