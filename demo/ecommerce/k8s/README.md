# `k8s/` — ecommerce on Rancher Desktop k3s

Kubernetes deployment of the ecommerce SUT. Replaces the Docker Compose
deployment for AIOps work; Compose remains fine for plain app development.

## Why k8s and not Compose

Compose runs the app correctly, but half the failure scenarios in
`../scenarios/` cannot be reproduced faithfully there:

| Need | Compose | k8s |
|---|---|---|
| OOMKill a service | container just dies | real `OOMKilled` + `restartCount` |
| CrashLoopBackOff | no such state | real backoff, real events |
| Kill one pod mid-request | `docker stop` (graceful) | `kubectl delete pod` |
| Agent remediation | shell commands | the kubectl verbs the agents already speak |
| Reach Jaeger / Loki | NodePort bridge | in-cluster DNS |

The remediation and runbook agents are kubectl-shaped, so running the SUT in
k8s means their actions are real rather than simulated.

## Deploy

```powershell
cd demo\ecommerce

.\k8s\build-images.ps1                 # build 5 images into the local daemon
kubectl apply -f k8s\00-namespace.yaml
kubectl apply -f k8s\01-config.yaml
kubectl apply -f k8s\10-datastores.yaml
kubectl -n ecommerce rollout status statefulset/mysql --timeout=180s
kubectl apply -f k8s\20-app.yaml
kubectl apply -f k8s\30-frontend.yaml
kubectl -n ecommerce get pods
```

Confirm logs are arriving (should return a non-empty result within a minute of
the first request):

```powershell
kubectl -n otel-demo port-forward svc/loki 3100:3100
curl "http://localhost:3100/loki/api/v1/query?query=%7Bservice_name%3D%22ecommerce-user-service%22%7D"
```

Databases are applied and awaited first because each service builds its
SQLAlchemy engine at import time — start them together and the app pods
crashloop until the databases happen to win the race.

## Access

| URL | What |
|---|---|
| <http://localhost:30080> | Frontend (store) |
| <http://localhost:30081> | user-service |
| <http://localhost:30082> | order-service |

`payment-service` and `mock-payment-gateway` are ClusterIP — nothing outside
the cluster calls them directly.

## Observability

No extra wiring, and no collection sidecars — each service talks to each backend
directly over in-cluster DNS:

| Signal | Path |
|---|---|
| Metrics | Prometheus scrapes the pod (`prometheus.io/scrape: "true"`, picked up by the chart's `kubernetes-pods` job) |
| Traces | app → `jaeger.observability.svc.cluster.local:4317` (OTLP/gRPC, native — no collector) |
| Logs | app → `loki.otel-demo.svc.cluster.local:3100/loki/api/v1/push` (see `src/observability/loki_handler.py`) |

Both write endpoints come from `01-config.yaml` (`OTEL_EXPORTER_OTLP_ENDPOINT`,
`LOKI_URL`); blanking either one disables that leg and the app keeps running.

Every workload carries a `service_name` label matching its `OTEL_SERVICE_NAME`,
and the log shipper sends that same value as its Loki `service_name` label — so
one label joins metrics, logs and traces in Grafana, and it is the label RA-007
Log Correlation queries on.

Logs still go to stdout as well, so `kubectl logs` works normally. That is
load-bearing for the OOMKill and CrashLoopBackOff scenarios: a pod that dies
between flushes must still leave its last lines somewhere.

> **Note on `job` labels:** pod discovery sets `job="kubernetes-pods"`, not
> `job="ecommerce"`. Alert rules therefore key on `namespace="ecommerce"`.
> See the `EcommerceServiceDown` rule in `demo/otel-demo/values.yaml`.

## Failure injection

Most scenarios flip a ConfigMap value and restart the workload:

```powershell
kubectl -n ecommerce patch cm ecommerce-config --type merge -p '{\"data\":{\"ORDER_INJECT_HTTP_500\":\"true\"}}'
kubectl -n ecommerce rollout restart deploy/order-service
```

Infrastructure-outage scenarios scale a datastore instead:

```powershell
kubectl -n ecommerce scale statefulset/mysql --replicas=0     # mysql_down
kubectl -n ecommerce scale statefulset/mysql --replicas=1     # recover
```

`order-service` has a 256Mi memory limit specifically so
`ORDER_INJECT_MEMORY_LEAK=true` reaches `OOMKilled` in demo-friendly time.

## Teardown

```powershell
kubectl delete namespace ecommerce
```

Deletes the PVCs too, so MySQL/Postgres/Redis data is lost. To keep data,
scale the workloads to 0 instead.

## Known gaps

- **Only the three Python services ship logs to Loki.** Direct push is a
  property of the application, so anything whose code we don't own is no longer
  collected. `mysql`, `postgres` and `redis` are upstream images; `frontend` is
  nginx serving a static SPA; `mock-payment-gateway` has no logging configured
  at all. Promtail used to sweep all of them off the node.

  This is a real loss, and it is the trade the direct-push model makes. It does
  not currently break a scenario — every truth file in `../truth_files/` keys its
  `expected_signals.logs` on a message the *application* emits (`user_service_mysql_down`
  wants user-service's own `"database connection failed"`, not MySQL's), and the
  datastore outages are still visible through `mysql_connection_status`,
  `redis_connection_status` and the pod's own error logs. `kubectl logs` also
  still works for every pod.

  If a future scenario needs datastore or nginx log lines as evidence, the fix
  is a Promtail DaemonSet scoped to *just those* workloads — not a return to
  tailing everything, which would double-store the three services that now push
  for themselves.
- The Compose static scrape config in `demo/otel-demo/values.yaml` must be
  deleted once the app runs only here, or every metric is counted twice.
- Scenario YAMLs in `../scenarios/` still describe `docker compose` commands
  and need porting to the kubectl equivalents (migration Phase 3).
