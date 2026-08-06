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
| Reach the OTel Collector | NodePort bridge | in-cluster DNS |

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

No extra wiring. Each app pod carries `prometheus.io/scrape: "true"`, so the
chart's existing `kubernetes-pods` job discovers it; logs reach Loki via the
OTel Collector; traces go to the collector over in-cluster DNS.

Every workload also carries a `service_name` label matching its
`OTEL_SERVICE_NAME`, so one label joins metrics, logs and traces in Grafana.

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

- The Compose static scrape config in `demo/otel-demo/values.yaml` must be
  deleted once the app runs only here, or every metric is counted twice.
- Scenario YAMLs in `../scenarios/` still describe `docker compose` commands
  and need porting to the kubectl equivalents (migration Phase 3).
