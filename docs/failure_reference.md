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

## Cheat sheet — inject, and what happens next

The one thing to internalise before anything else: **8 of 17 failures show nothing
without traffic.** They break a request path, so with no requests in flight there
is nothing to be slow or to fail. Those are marked ⚠️ and need `--load <seconds>`
(or your own traffic) or you will conclude the injection did nothing.

Settle time is ~20s for every scenario; alerts add their own `for:` window on top
(15s or 30s), so budget **30–60s from inject to alert** for most, longer for the
ones that need load to accumulate.

### Application layer (8) — env vars, ConfigMaps, scale-to-zero

| Failure | Full inject command | What you'll see, and when |
|---|---|---|
| `user_service.mysql_down` | `uv run python -m demo.ecommerce.failure_injection inject user_service.mysql_down` | MySQL scaled to 0. `/login` 500s immediately; `mysql_connection_status`→0 in one scrape; **EcommerceMySQLDown ~30s** |
| `user_service.high_cpu` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject user_service.high_cpu --load 120` | Burns CPU inside each request. CPU → ~0.85 of 1 core; `/login` slows; **EcommerceUserServiceCPUHigh ~60s** |
| `user_service.crashloop` | `uv run python -m demo.ecommerce.failure_injection inject user_service.crashloop` | `MYSQL_HOST` unresolvable → dies before uvicorn binds. `CrashLoopBackOff`, `restartCount` climbing, **no HTTP logs at all**; **EcommerceServiceDown ~30s** |
| `order_service.postgres_down` | `uv run python -m demo.ecommerce.failure_injection inject order_service.postgres_down` | Postgres scaled to 0. `/orders` 500s; gauge→0; **EcommercePostgresDown ~30s** |
| `order_service.memory_leak_oom` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject order_service.memory_leak_oom --load 120` | App heap grows with order volume. **EcommerceOrderServiceMemoryHigh on the climb**, then **OOMKilled** + `restartCount` ↑ + brief `ServiceDown` |
| `payment_service.redis_down` | `uv run python -m demo.ecommerce.failure_injection inject payment_service.redis_down` | Redis scaled to 0. `/payments` 500s; gauge→0; **EcommerceRedisDown ~30s** |
| `payment_service.gateway_timeout` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject payment_service.gateway_timeout --load 120` | Gateway delayed 30s. `/payments` 504; `payment_failures_total{gateway_timeout}` rises; **EcommercePaymentTimeouts ~30s** |
| `payment_service.http_500` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject payment_service.http_500 --load 120` | Every charge fails. `/payments` 500s; surfaces downstream as `orders_failed_total{reason=payment_failed}`; **EcommerceOrderErrorRateHigh ~30s** |

### Hybrid (4) — both layers fire; add `--mode application` or `--mode infrastructure` to pick one

| Failure | Full inject command | What you'll see, and when |
|---|---|---|
| `user_service.high_latency` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject user_service.high_latency --load 120` | App half sleeps 10s per `/login`; infra half would add 500ms of `tc` delay but is **unavailable** (no `tc`) → reports `degraded`. Order p95 crosses 2s; **EcommerceOrderLatencyHigh ~60s**. DB + CPU normal ← discriminator |
| `order_service.payment_timeout` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject order_service.payment_timeout --load 120` | Gateway delayed 30s past order's 5s timeout; infra half **unavailable** (no `tc`). `/orders` 504 after ~5s; **EcommercePaymentTimeouts ~30s**. Fault is on the **gateway**, not order-service |
| `order_service.http_500` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject order_service.http_500 --load 120` | Env flag **and** the payment pod is killed — both halves work. `/orders` 500s; **EcommerceOrderErrorRateHigh ~30s**; payment pod replaced then Ready |
| `payment_service.high_cpu` ⚠️ | `uv run python -m demo.ecommerce.failure_injection inject payment_service.high_cpu --load 120` | Env flag **and** a Python burn at 85% of one core — both halves work. CPU ~0.85 cores; **EcommercePaymentServiceCPUHigh ~60s**. Pod **stays Ready**: the 85% cap is deliberate, a flat spin starves `/health` past its 1s probe and the kubelet restarts the container |

### Infrastructure layer (5) — real chaos, no env var involved

| Failure | Full inject command | What you'll see, and when |
|---|---|---|
| `user_service.pool_exhaustion` | `uv run python -m demo.ecommerce.failure_injection inject user_service.pool_exhaustion` | Holds 155 MySQL sessions from inside the pod. `Threads_connected` pins at 151; `/login` 500s with `Too many connections`; **EcommerceUserLoginFailures ~30s**. **MySQL itself stays Up** — user-service is the victim |
| `order_service.memory_exhaust` | `uv run python -m demo.ecommerce.failure_injection inject order_service.memory_exhaust` | External process holds 200MB resident. Memory pins at ~255 of 256Mi; **EcommerceOrderServiceMemoryHigh ~30s**. **No OOMKill, no restart** — the kernel reclaims. App heap looks normal |
| `payment_service.disk_full` | `uv run python -m demo.ecommerce.failure_injection inject payment_service.disk_full` | 256MB file written to `/tmp`. `df` free drops by exactly 256MB — **and nothing else**. 0.025% of a 1TB shared filesystem; **no alert, by decision** |
| `payment_service.dns_failure` | `uv run python -m demo.ecommerce.failure_injection inject payment_service.dns_failure` | `/etc/resolv.conf` poisoned. Name resolution fails; **EcommercePaymentGatewayUnreachable ~30s** — **plus a misleading EcommerceRedisDown**, because payment re-pings Redis in `/metrics` and zeroes the gauge on any exception. Redis is **healthy and Ready** |
| `order_service.packet_loss` | ❌ **cannot inject on this cluster** | 5% loss via `tc netem`. Needs `iproute2` in the image **and** `CAP_NET_ADMIN` in the pod spec; neither is present, so it fails outright rather than degrading |

Recover any of them by swapping `inject` for `recover`, e.g.
`uv run python -m demo.ecommerce.failure_injection recover payment_service.dns_failure`.

Three recoveries are non-obvious: `memory_exhaust` and `high_cpu` kill the offending
process **without restarting the pod**, because a restart would erase the
`restartCount` and OOMKilled evidence the truth files assert on; `dns_failure`
does restart the pod, since that is what makes the kubelet rewrite `resolv.conf`.

All twelve rules are deployed. Rows 3, 5, 9, 11, 14 and 17 used to name alerts that
existed in `prometheus-values.yaml` but had never been pushed to the running
Prometheus — they injected and behaved exactly as described but did not page.
`helm upgrade` has since run; the live rule count is 12, matching the file.

Alertmanager now delivers, too. Its receiver was previously `default-receiver`
with no destination configured, so every alert Prometheus produced was accepted
and dropped — the pipeline looked healthy from every angle and notified nobody.
It now routes to Slack via a catch-all route (no severity matcher, deliberately:
a filtered route silently drops whatever severity it does not match).

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
| 3 | `high_cpu` | `/login` slows under load | CPU ≈0.85 cores → **EcommerceUserServiceCPUHigh**; `login_latency_seconds` ↑ | nothing distinctive | one process pegged; `kubectl top pod` |
| 4 | `crashloop` | service never becomes reachable | `up{namespace="ecommerce"} == 0` → **EcommerceServiceDown** | startup logs: DB resolution failure, then exit | `CrashLoopBackOff`, `restartCount` climbing |
| 5 | `pool_exhaustion` | 500 on `/login`, intermittent | `login_failure_total{db_error}` ↑ → **EcommerceUserLoginFailures**; `mysql_connection_status` **flapping** | `Too many connections` on connect | MySQL `Threads_connected` pinned at 151; app's own pool healthy |
| 6 | `postgres_down` | 500 on `POST /orders` | `postgres_connection_status == 0`; `orders_failed_total{reason=db_error}` ↑ → **EcommercePostgresDown** | `connection refused to postgres:5432` | `statefulset/postgres` replicas 0 |
| 7 | `payment_timeout` | 504 on `POST /orders` after ~5s | `payment_timeout_total` ↑ → **EcommercePaymentTimeouts**; `order_latency_seconds` ↑ | timeout on the payment call | **Jaeger:** order→payment span stalls |
| 8 | `http_500` (order) | 500 on `POST /orders` | `orders_failed_total` ↑ → **EcommerceOrderErrorRateHigh** | app half: injected error. infra half: `connection refused` to payment | infra half: payment pod gone then replaced |
| 9 | `memory_leak_oom` | `/orders` fails after sustained load | memory → limit → **EcommerceOrderServiceMemoryHigh** (fires on the climb) | OOM kill message | `restartCount` ↑ with `terminated.reason=OOMKilled` |
| 10 | `packet_loss` | intermittent `/orders` connection errors | `orders_failed_total` ↑ | connection reset / retry lines | TCP retransmits ↑ (`tcpdump`); `tc qdisc show` |
| 11 | `memory_exhaust` | app keeps serving | working_set **pinned at 256Mi** → **EcommerceOrderServiceMemoryHigh** | app heap metrics normal — pressure is external | **no restart, no OOMKill** (kernel reclaims); measured 81 → 255 MiB |
| 12 | `redis_down` | 500 on `POST /payments` | `redis_connection_status == 0`; `payment_failures_total{reason=redis_error}` ↑ → **EcommerceRedisDown** | `connection refused to redis:6379` | `statefulset/redis` replicas 0 |
| 13 | `gateway_timeout` | 504 on `POST /payments` | `payment_latency_seconds` ↑; `payment_failures_total{reason=gateway_timeout}` ↑ — but the alert comes from order-service's `payment_timeout_total` → **EcommercePaymentTimeouts** (see the coverage note below) | gateway timeout lines | **Jaeger:** payment→gateway span stalls |
| 14 | `high_cpu` (payment) | `/payments` slows under load | CPU ≈0.85 cores → **EcommercePaymentServiceCPUHigh**; `payment_latency_seconds` ↑ | nothing distinctive | pod stays Ready, `restartCount` unchanged (this is by design) |
| 15 | `http_500` (payment) | 500 on `POST /payments` | `payment_failures_total{reason=injected_500}` ↑ → **EcommerceOrderErrorRateHigh** | injected failure lines | — |
| 16 | `disk_full` | write paths may fail | filesystem available bytes ↓ 256MB | write errors | ⚠️ **256MB of a 1006GB shared fs = 0.025%. Not a detectable signal.** See caveat below |
| 17 | `dns_failure` | payment calls fail to connect | `payment_failures_total{gateway_error}` ↑ → **EcommercePaymentGatewayUnreachable**, **plus a misleading EcommerceRedisDown** | `getaddrinfo` failures | `cat /etc/resolv.conf` shows `nameserver 1.2.3.4`; Redis itself is Ready |

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

Five more rules cover resource saturation:

| Alert | Expression | Covers |
| --- | --- | --- |
| `EcommerceUserServiceCPUHigh` | `sum by (pod)(rate(container_cpu_usage_seconds_total{pod=~"user-service-.*"}[1m])) > 0.5` | #3 |
| `EcommercePaymentServiceCPUHigh` | same, `pod=~"payment-service-.*"` | #14 |
| `EcommerceOrderServiceMemoryHigh` | `working_set / (spec_limit > 0) > 0.9`, `pod=~"order-service-.*"` | #9, #11 |
| `EcommerceUserLoginFailures` | `sum(rate(login_failure_total{reason="db_error"}[2m])) > 0` | #1, #5 |
| `EcommercePaymentGatewayUnreachable` | `sum(rate(payment_failures_total{reason="gateway_error"}[2m])) > 0` | #17 |

Three details these depend on:

- **Keyed on `pod`, not `container`.** This cluster's cAdvisor publishes
  pod-level rollups carrying `id`/`job`/`namespace`/`pod` and no `container`
  label, so a `container="user-service"` selector matches nothing and the rule
  never fires. Verified live: pod-keyed returns 9 series, container-keyed 0.
- **`[1m]` for CPU, not `[2m]`.** A 120s burst capped at 0.85 cores, averaged
  over a 2m window, peaks around 0.43 — below any threshold worth setting.
- **`reason="db_error"` is a positive pin.** `login_failure_total` also counts
  `invalid_credentials`, which every load generator produces by design, so an
  unfiltered rate would fire with no fault injected at all.

**Disk (#16) has no rule, deliberately** — see the caveat below. That leaves
`payment_service.disk_full` as the only injectable failure that cannot page.
(#10 `packet_loss` also never pages, but only because it cannot be injected on
this cluster at all — there is no fault to detect.)

### `payment_failures_total` is covered for one `reason` out of four

`EcommercePaymentGatewayUnreachable` is the only rule that reads
`payment_failures_total`, and it pins `reason="gateway_error"`. The other three
values have no rule of their own:

| `reason` | Raised by | Alerting path |
| --- | --- | --- |
| `gateway_error` | #17 `dns_failure` | **direct** — `EcommercePaymentGatewayUnreachable` |
| `redis_error` | #12 `redis_down` | **direct**, but via a different signal — `redis_connection_status == 0` |
| `gateway_timeout` | #13 `gateway_timeout` | **indirect only** — order-service's `payment_timeout_total` → `EcommercePaymentTimeouts` |
| `injected_500` | #15 `payment_service.http_500` | **indirect only** — `orders_failed_total{reason=payment_failed}` → `EcommerceOrderErrorRateHigh` |

The two indirect rows are the ones to know about: **those failures are detected
by the caller, not by the faulty service.** Both alert only while traffic is
flowing through `POST /orders`. Traffic that hits `POST /payments` directly —
bypassing order-service — exercises the fault and pages nobody.

In practice the `loadgen` Deployment
(`demo/ecommerce/k8s/40-loadgen.yaml`) drives full `/orders` → payment chains
continuously, so both do fire during a normal demo. The gap matters if you ever
drive payment-service in isolation, or scale `loadgen` to zero and hand-test.

---

## Application metrics, by service

| Service | Metrics |
| --- | --- |
| user-service | `login_requests_total`, `login_failure_total`, `login_latency_seconds`, `mysql_connection_status` |
| order-service | `orders_created_total`, `orders_failed_total`, `order_latency_seconds`, `payment_timeout_total`, `postgres_connection_status` |
| payment-service | `payment_requests_total`, `payment_failures_total`, `payment_latency_seconds`, `redis_connection_status` |

## Useful queries

**Loki** (`http://localhost:30310`, or the Grafana Loki datasource):

The stream labels are `namespace`, `container`, `pod`, `service_name` and
`filename` — there is **no `app` label**, so `{app="user-service"}` matches
nothing and returns an empty result rather than an error. Use `container` (bare
service name) or `service_name` (the `OTEL_SERVICE_NAME`, prefixed
`ecommerce-`), which is the same value the metrics and traces carry.

```logql
{namespace="ecommerce", container="user-service"} |= "connection"
{namespace="ecommerce"} |= "Too many connections"
{namespace="ecommerce"} |= "getaddrinfo"
{namespace="ecommerce", container="order-service"} |= "timeout"

# equivalent, joined on the value shared with metrics and traces:
{namespace="ecommerce", service_name="ecommerce-user-service"}

# case-insensitive error sweep. Note `|= "A" or |= "B"` is NOT valid LogQL —
# Loki returns HTTP 400 — so chain a regex filter instead:
{namespace="ecommerce"} |~ "(?i)error|exception|traceback"
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

- **`dns_failure` also fires `EcommerceRedisDown`, which is a lie.**
  payment-service calls `store.ping()` inside its `/metrics` handler, and
  `redis_client.ping()` zeroes `redis_connection_status` on *any* exception —
  including a `getaddrinfo` failure. So breaking DNS drives the Redis gauge to 0
  within one scrape while Redis is perfectly healthy and Ready. Two alerts
  firing together, with `kubectl get statefulset redis` showing
  `readyReplicas=1`, is the DNS fingerprint. The RCA prompt teaches this
  explicitly; the alert rule was left alone rather than adding an `unless`
  clause to a proven rule that a working scenario depends on.
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
  Since `demo/ecommerce/k8s/40-loadgen.yaml` was added there is also a permanent
  baseline — a `loadgen` Deployment driving one full `/login` → `/orders` →
  payment session every ~5s — so the ⚠️ rows now fire on their own, more slowly
  than with `--load` but without any manual step. Check it is actually running
  (`kubectl -n ecommerce get deploy loadgen`) before concluding an injection did
  nothing: `reset.ps1 -Data` pauses it mid-run, and it is easy to leave scaled
  to zero.
