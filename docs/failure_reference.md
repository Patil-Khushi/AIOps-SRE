# Failure reference: inject, observe, recover

One row per registered failure. Every metric name, alert name, port and command
below was read out of the code or the live cluster — nothing here is illustrative.

Companion to [chaos_engineering.md](chaos_engineering.md), which covers the
two-layer architecture and the safety rationale.

## Access

| What | Where |
| --- | --- |
| user-service | `http://localhost:30081` |
| order-service | `http://localhost:30082` |
| payment-service | `http://localhost:30083` |
| frontend | `http://localhost:30080` |
| Loki (ecommerce) | `http://localhost:30310` |
| Grafana | ClusterIP — `kubectl -n observability port-forward svc/grafana 3000:80` |
| Prometheus | ClusterIP — `kubectl -n observability port-forward svc/prometheus-server 9090:80` |
| Alertmanager | ClusterIP — `kubectl -n observability port-forward svc/prometheus-alertmanager 9093:9093` |

`CMD` below is shorthand for:

```bash
uv run python -m demo.ecommerce.failure_injection
```

Add `--mode application` / `--mode infrastructure` to pick a layer; the default
is `hybrid`. Add `--load 30` to drive traffic for 30s after injecting — the
CPU, latency and OOM scenarios need traffic before they show anything. Recover
any failure with `CMD recover <key>`.

---

## Table A — How to inject

| # | Failure | Layer | Inject | Mechanism | Prereq |
| --- | --- | --- | --- | --- | --- |
| 1 | `user_service.mysql_down` | app | `CMD inject user_service.mysql_down` | scale `statefulset/mysql` → 0 | — |
| 2 | `user_service.high_latency` | hybrid | `CMD inject user_service.high_latency` | `INJECT_LATENCY_SECONDS=10` **+** tc netem delay 500ms | tc half needs `iproute2` + `NET_ADMIN` |
| 3 | `user_service.high_cpu` | app | `CMD inject user_service.high_cpu --load 60` | `INJECT_CPU_LOAD=true` | needs load |
| 4 | `user_service.crashloop` | app | `CMD inject user_service.crashloop` | `MYSQL_HOST=nonexistent-db-host` | — |
| 5 | `user_service.pool_exhaustion` | infra | `CMD inject user_service.pool_exhaustion` | hold 155 MySQL sessions via pymysql | — |
| 6 | `order_service.postgres_down` | app | `CMD inject order_service.postgres_down` | scale `statefulset/postgres` → 0 | — |
| 7 | `order_service.payment_timeout` | hybrid | `CMD inject order_service.payment_timeout` | gateway `INJECT_DELAY_SECONDS=30` **+** tc delay 30s on payment | tc half needs `iproute2` + `NET_ADMIN` |
| 8 | `order_service.http_500` | hybrid | `CMD inject order_service.http_500` | `INJECT_HTTP_500=true` **+** `kubectl delete pod payment-service` | — |
| 9 | `order_service.memory_leak_oom` | app | `CMD inject order_service.memory_leak_oom --load 120` | `INJECT_MEMORY_LEAK=true` | needs load |
| 10 | `order_service.packet_loss` | infra | `CMD inject order_service.packet_loss` | tc netem loss 5% | **`iproute2` + `NET_ADMIN`** |
| 11 | `order_service.memory_exhaust` | infra | `CMD inject order_service.memory_exhaust` | hold 200MB resident vs 256Mi limit | — |
| 12 | `payment_service.redis_down` | app | `CMD inject payment_service.redis_down` | scale `statefulset/redis` → 0 | — |
| 13 | `payment_service.gateway_timeout` | app | `CMD inject payment_service.gateway_timeout` | gateway `INJECT_DELAY_SECONDS=30` | — |
| 14 | `payment_service.high_cpu` | hybrid | `CMD inject payment_service.high_cpu --load 60` | `INJECT_CPU_LOAD=true` **+** python burn 1 core @ 85% | needs load |
| 15 | `payment_service.http_500` | app | `CMD inject payment_service.http_500` | `INJECT_HTTP_500=true` | — |
| 16 | `payment_service.disk_full` | infra | `CMD inject payment_service.disk_full` | `dd` 256MB into `/tmp` | — |
| 17 | `payment_service.dns_failure` | infra | `CMD inject payment_service.dns_failure` | overwrite `/etc/resolv.conf` | — |

Rows 2, 7, 10 are the only ones with an unmet prerequisite today. Rows 2 and 7
still work — they fall back to their application half and report `degraded`.
Row 10 is infrastructure-only, so it fails outright until `tc` is available.

---

## Table B — What to observe

| # | Failure | In the application | Prometheus | Loki | Pod / infra state |
| --- | --- | --- | --- | --- | --- |
| 1 | `mysql_down` | 500 on `POST /login` | `mysql_connection_status == 0`; `login_failure_total` ↑ → **EcommerceMySQLDown** | `connection refused to mysql:3306` | `statefulset/mysql` replicas 0 |
| 2 | `high_latency` | `/login` takes ~10s (app half) or +500ms (tc half) | `login_latency_seconds` p95/p99 ↑; `order_latency_seconds` p95 > 2 → **EcommerceOrderLatencyHigh** | slow-request lines on login handler | DB + CPU normal — that's the discriminator |
| 3 | `high_cpu` | `/login` slows under load | container CPU > 90%; `login_latency_seconds` ↑ | nothing distinctive | one process pegged; `kubectl top pod` |
| 4 | `crashloop` | service never becomes reachable | `up{namespace="ecommerce"} == 0` → **EcommerceServiceDown** | startup logs: DB resolution failure, then exit | `CrashLoopBackOff`, `restartCount` climbing |
| 5 | `pool_exhaustion` | 500 on `/login`, intermittent | `login_failure_total` ↑; `mysql_connection_status` **flapping** | `Too many connections` on connect | MySQL `Threads_connected` pinned at 151; app's own pool healthy |
| 6 | `postgres_down` | 500 on `POST /orders` | `postgres_connection_status == 0`; `orders_failed_total{reason=db_error}` ↑ → **EcommercePostgresDown** | `connection refused to postgres:5432` | `statefulset/postgres` replicas 0 |
| 7 | `payment_timeout` | 504 on `POST /orders` after ~5s | `payment_timeout_total` ↑ → **EcommercePaymentTimeouts**; `order_latency_seconds` ↑ | timeout on the payment call | **Tempo:** order→payment span stalls |
| 8 | `http_500` (order) | 500 on `POST /orders` | `orders_failed_total` ↑ → **EcommerceOrderErrorRateHigh** | app half: injected error. infra half: `connection refused` to payment | infra half: payment pod gone then replaced |
| 9 | `memory_leak_oom` | `/orders` fails after sustained load | container memory climbing to limit | OOM kill message | `restartCount` ↑ with `terminated.reason=OOMKilled` |
| 10 | `packet_loss` | intermittent `/orders` connection errors | `orders_failed_total` ↑ | connection reset / retry lines | TCP retransmits ↑ (`tcpdump`); `tc qdisc show` |
| 11 | `memory_exhaust` | app keeps serving | `container_memory_working_set_bytes` **pinned at 256Mi** | app heap metrics normal — pressure is external | **no restart, no OOMKill** (kernel reclaims); measured 81 → 255 MiB |
| 12 | `redis_down` | 500 on `POST /payments` | `redis_connection_status == 0`; `payment_failures_total{reason=redis_error}` ↑ → **EcommerceRedisDown** | `connection refused to redis:6379` | `statefulset/redis` replicas 0 |
| 13 | `gateway_timeout` | 504 on `POST /payments` | `payment_latency_seconds` ↑; `payment_failures_total{reason=gateway_timeout}` ↑ → **EcommercePaymentTimeouts** | gateway timeout lines | **Tempo:** payment→gateway span stalls |
| 14 | `high_cpu` (payment) | `/payments` slows under load | container CPU ~85–90%; `payment_latency_seconds` ↑ | nothing distinctive | pod stays Ready, `restartCount` unchanged (this is by design) |
| 15 | `http_500` (payment) | 500 on `POST /payments` | `payment_failures_total{reason=injected_500}` ↑ → **EcommerceOrderErrorRateHigh** | injected failure lines | — |
| 16 | `disk_full` | write paths may fail | filesystem available bytes ↓ 256MB | write errors | ⚠️ **256MB of a 1006GB shared fs = 0.025%. Not a detectable signal.** See caveat below |
| 17 | `dns_failure` | payment calls fail to connect | connection errors ↑ | `getaddrinfo` failures | `cat /etc/resolv.conf` shows `nameserver 1.2.3.4` |

### Alert names in full

Defined in `infra/observability/prometheus-values.yaml`:

| Alert | Expression |
| --- | --- |
| `EcommerceMySQLDown` | `mysql_connection_status == 0` |
| `EcommercePostgresDown` | `postgres_connection_status == 0` |
| `EcommerceRedisDown` | `redis_connection_status == 0` |
| `EcommercePaymentTimeouts` | `rate(payment_timeout_total[2m]) > 0` |
| `EcommerceOrderErrorRateHigh` | `sum by (reason) (rate(orders_failed_total[2m])) > 0` |
| `EcommerceOrderLatencyHigh` | `histogram_quantile(0.95, sum by (le) (rate(order_latency_seconds_bucket[2m]))) > 2` |
| `EcommerceServiceDown` | `up{namespace="ecommerce"} == 0` |

Note there is **no alert rule for CPU, memory, disk or DNS**. Failures 3, 9, 11,
14, 16 and 17 will show in metrics and logs but will not page anyone. If those
scenarios are meant to drive the RCA agent from an alert, rules need adding.

---

## Application metrics, by service

| Service | Metrics |
| --- | --- |
| user-service | `login_requests_total`, `login_failure_total`, `login_latency_seconds`, `mysql_connection_status` |
| order-service | `orders_created_total`, `orders_failed_total`, `order_latency_seconds`, `payment_timeout_total`, `postgres_connection_status` |
| payment-service | `payment_requests_total`, `payment_failures_total`, `payment_latency_seconds`, `redis_connection_status` |

## Useful queries

**Loki** (`http://localhost:30310`, or the Grafana Loki datasource):

```logql
{namespace="ecommerce", app="user-service"} |= "connection"
{namespace="ecommerce"} |= "Too many connections"
{namespace="ecommerce"} |= "getaddrinfo"
{namespace="ecommerce", app="order-service"} |= "timeout"
```

**Prometheus:**

```promql
mysql_connection_status or postgres_connection_status or redis_connection_status
histogram_quantile(0.95, sum by (le) (rate(order_latency_seconds_bucket[2m])))
sum by (reason) (rate(orders_failed_total[2m]))
container_memory_working_set_bytes{namespace="ecommerce"}
rate(container_cpu_usage_seconds_total{namespace="ecommerce"}[2m])
```

**Pod state** (for the OOM, crashloop and pod-kill scenarios):

```bash
kubectl -n ecommerce get pods -w
kubectl -n ecommerce get pod <pod> -o jsonpath='{.status.containerStatuses[0].lastState.terminated.reason}'
kubectl -n ecommerce describe pod <pod> | tail -20
```

**MySQL connections** (for `pool_exhaustion`):

```bash
kubectl -n ecommerce exec mysql-0 -- sh -c \
  'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -B -e "SHOW STATUS LIKE '"'"'Threads_connected'"'"';"'
```

---

## Caveats that change what you'll see

- **`disk_full` produces no usable signal.** 256MB against the 1006GB node
  overlay is 0.025%. Fixing it means mounting an `emptyDir` with a `sizeLimit`
  and pointing `TARGET_PATH` at it — not raising the fill size, which would mean
  writing ~950GB to the filesystem etcd lives on.
- **`memory_exhaust` does not OOMKill.** Measured 81 → 255 MiB against the 256Mi
  limit with `restartCount` unchanged: the kernel reclaimed rather than killing.
  You get sustained at-limit pressure, not a restart. For a guaranteed
  container-level `OOMKilled`, use `order_service.memory_leak_oom` (#9).
- **`high_cpu` (payment) deliberately stops at 85%.** A flat 100% spin starves
  `/health` past its 1s probe timeout and the kubelet restarts the container,
  which turns CPU saturation into a crashloop. Expect ~85–90%, not 100%.
- **Rows 2, 7, 10 need `tc`.** Not installed, and the pods lack `CAP_NET_ADMIN`
  (`CapEff=0xa80425fb`). 2 and 7 degrade to their application half; 10 fails.
- **CPU / latency / OOM scenarios need traffic.** Without `--load`, there is
  nothing to be slow. Use the built-in load driver or hit the endpoint yourself.
