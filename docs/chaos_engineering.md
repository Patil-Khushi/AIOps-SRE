# Failure injection: application layer + infrastructure chaos

Failure injection runs through one orchestrator that can drive two independent
layers. This document records what each layer does, **what actually works on the
current cluster**, and what is blocked pending image or manifest changes.

```
                    Failure Orchestrator            demo/ecommerce/failure_injection/_orchestrator.py
                            │
         ┌──────────────────┴──────────────────┐
         ▼                                     ▼
  Application layer                     Infrastructure layer
  env vars · ConfigMaps ·               tc · python stress · kubectl ·
  scale-to-zero                         dd · DNS · connection hold
         │                                     │
         └──────────────────┬──────────────────┘
                            ▼
              Prometheus · Loki · Tempo
                            ▼
                     AI RCA agent
```

## Choosing a layer

`--mode` on the CLI, else `FI_MODE`, else `hybrid`.

| Mode | Runs |
| --- | --- |
| `application` | env vars, ConfigMaps, scale-to-zero |
| `infrastructure` | tc, python stress, pod kills, dd, DNS |
| `hybrid` *(default)* | both, for failures that implement both |

The orchestrator intersects the requested mode with each failure's declared
`Failure.layer`. Asking for a layer a failure does not implement runs **nothing**
and returns `ok=False` — it does not silently succeed. That matters because an
infrastructure-only failure keeps its chaos action in `inject` (there is no
second implementation to put in `inject_infra`), so dispatching on the requested
mode alone would fire real chaos at an operator who explicitly asked for the
env-var path.

### Per-layer outcomes

Each layer reports one of four statuses, and `ok` is *not* simply their AND:

| Status | Meaning | Counts as success? |
| --- | --- | --- |
| `ran` | the layer did its work | yes |
| `skipped` | this failure does not implement the layer | n/a |
| `unavailable` | the environment cannot host it (no `tc`, no `CAP_NET_ADMIN`) | yes, if a sibling layer ran |
| `error` | the layer genuinely failed | no |

`ok` is true when **at least one layer ran** and none errored. `degraded` is set
when something ran but a sibling was unavailable.

The distinction is load-bearing. Default mode is `hybrid`, and the four hybrid
failures currently have an unavailable `tc` half on this cluster — so treating
`unavailable` as an error would report a *successfully injected* fault as failed,
and [demo/ui/scenario_provider.py](../demo/ui/scenario_provider.py) reads `ok`
straight through to the dashboard. An operator would go chasing a fault that is
in fact active. Conversely, when *nothing* ran, `ok` is false: a HITL "fix" that
quietly did nothing must never read as success.

```bash
uv run python -m demo.ecommerce.failure_injection list --show-layers
uv run python -m demo.ecommerce.failure_injection inject user_service.high_latency --mode hybrid
uv run python -m demo.ecommerce.failure_injection recover user_service.high_latency
FI_DRY_RUN=1 uv run python -m demo.ecommerce.failure_injection inject order_service.packet_loss
```

## Failure inventory (17)

Twelve app-layer failures, four of which now also carry an infrastructure
implementation (`hybrid`), plus five infrastructure-only additions.

| Key | Layer | Application mechanism | Infrastructure mechanism |
| --- | --- | --- | --- |
| `user_service.mysql_down` | application | scale StatefulSet to 0 | — |
| `user_service.high_latency` | **hybrid** | `INJECT_LATENCY_SECONDS=10` | tc netem delay 500ms |
| `user_service.high_cpu` | application | `INJECT_CPU_LOAD=true` | — |
| `user_service.crashloop` | application | `MYSQL_HOST=nonexistent` | — |
| `user_service.pool_exhaustion` | infrastructure | — | hold 155 MySQL sessions (pymysql) |
| `order_service.postgres_down` | application | scale StatefulSet to 0 | — |
| `order_service.payment_timeout` | **hybrid** | `INJECT_DELAY_SECONDS=30` | tc netem delay 30s on payment |
| `order_service.http_500` | **hybrid** | `INJECT_HTTP_500=true` | `kubectl delete pod payment-service` |
| `order_service.memory_leak_oom` | application | `INJECT_MEMORY_LEAK=true` | — |
| `order_service.packet_loss` | infrastructure | — | tc netem loss 5% |
| `order_service.memory_exhaust` | infrastructure | — | hold 200MB resident |
| `payment_service.redis_down` | application | scale StatefulSet to 0 | — |
| `payment_service.gateway_timeout` | application | `INJECT_DELAY_SECONDS=30` | — |
| `payment_service.high_cpu` | **hybrid** | `INJECT_CPU_LOAD=true` | python burn, 1 core @ 85% |
| `payment_service.http_500` | application | `INJECT_HTTP_500=true` | — |
| `payment_service.disk_full` | infrastructure | — | `dd` a 256MB file |
| `payment_service.dns_failure` | infrastructure | — | overwrite `/etc/resolv.conf` |

`__init__.py` raises on duplicate keys at import: `FAILURES` is a flat dict, so
two modules claiming one key would silently shadow each other and leave the loser
unreachable from the CLI, dashboard and RCA agent alike.

## What the container images actually provide

Probed against the running `ecommerce` namespace. These are slim Python images:

| Tool | Present | Used by |
| --- | --- | --- |
| `python3` | ✅ | CPU burn, memory hog, PID tracking |
| `pymysql` | ✅ | connection exhaustion (SQLAlchemy's driver) |
| `mysql-connector-python` | ❌ | not used — `pymysql` covers it |
| `dd` | ✅ | disk fill |
| `sh` | ✅ | everything |
| `tc` (iproute2) | ❌ | **network latency, packet loss** |
| `ip` | ❌ | interface discovery (works around via sysfs) |
| `stress-ng` | ❌ | not used — replaced by python |
| `ps` / `pkill` (procps) | ❌ | not used — replaced by a pidfile |

Two consequences drove the implementation:

- **No `stress-ng`.** CPU and memory stress run as plain `python3 -c` payloads.
  A busy multiply-mod loop is indistinguishable from `stress-ng --cpu` as far as
  cgroup CPU accounting and the resulting Prometheus series are concerned, and it
  avoids rebuilding every service image to run one scenario. One process per
  core, because the GIL prevents a single process from saturating more than one.
- **No `procps`.** Backgrounded PIDs are appended to `/tmp/aiops-chaos.pids` and
  killed via `os.kill` from python. `stop_stress()` is preferred over killing the
  pod, because a restart resets `restartCount` and clears the OOMKilled
  terminated-reason that the truth files assert on.

## Blocked: network chaos

`tc`-based failures — `order_service.packet_loss` and the infrastructure half of
`user_service.high_latency` and `order_service.payment_timeout` — **cannot run as
deployed**, for two independent reasons:

1. `tc` is not in the images.
2. The pods lack `CAP_NET_ADMIN` (`CapEff=0xa80425fb`, the containerd default set,
   which grants `CAP_NET_RAW` but not `CAP_NET_ADMIN`). `tc qdisc add` returns
   EPERM without it even when `tc` is installed.

Preflight (`require_binary`, `require_net_admin`) raises `ChaosUnavailable` with
the remedy rather than letting `kubectl exec` fail with a bare exit 127 — the fix
is not inferable from the exit code.

Two ways to unblock, whichever suits:

**A. Change the images and manifests.** Add `iproute2` to each service Dockerfile,
then grant the capability in `k8s/20-app.yaml` per container:

```yaml
securityContext:
  capabilities:
    add: ["NET_ADMIN"]
```

**B. Use Chaos Mesh.** Its privileged DaemonSet injects into the pod's network
namespace from the host, so no per-image or per-manifest change is needed. This
is the better fit if network chaos is wanted across more services later.

## Safety notes

- **Disk fill is bounded, not percentage-based.** `MAX_DISK_FILL_MB = 512` caps
  every write. These pods have no dedicated data volume: `/` is the containerd
  overlay, which on Rancher Desktop is the *node's* ~1 TB filesystem shared with
  etcd and every other pod. A "fill to 95%" there means writing ~950 GB and can
  take the cluster down. The honest trade-off: a bounded write to a 1 TB shared
  filesystem barely moves `disk_usage_percent`. For a real DiskPressure signal,
  mount an `emptyDir` with a `sizeLimit` into the pod and target that.
- **Stress payloads self-expire.** Every burn, hog and connection holder carries
  its own timeout, so a missed `recover()` heals on its own rather than leaving
  the cluster loaded.
- **CPU stress is duty-cycled, not a flat spin.** Containers have `limits.cpu: 1`
  and a liveness probe of `timeout=1s, failureThreshold=3`. A 100% spin starves
  `/health` and the kubelet restarts the container about a minute in, turning CPU
  saturation into a crashloop — the wrong signal, and it kills the burn with the
  container. 85% of one core keeps `/health` answerable while still pinning the
  CPU series. Raising `CORES` above the cpu limit only deepens throttling; it
  does not raise the metric.
- **`memory_exhaust` does not OOMKill at the current size — it pins at the limit.**
  Measured: cgroup memory went 81 → 255 MiB against the 256Mi limit, with
  `restartCount` unchanged and no `terminated.reason`. No OOM fired because the
  kernel had reclaimable memory (page cache, the app's cold pages) and freed that
  instead of killing anything. The result is a *sustained at-limit* signal rather
  than a kill — arguably the more useful scenario, since nothing restarts and the
  pressure persists for the whole window.

  To force an actual OOMKill, raise `MEMORY_MB` until the allocation outpaces
  reclaim (try 260–300 against the 256Mi limit). Be aware the cgroup OOM killer
  picks by `oom_score`, so it will likely take the hog rather than the app, which
  still yields no pod-level `OOMKilled`. For a guaranteed container-level
  `OOMKilled`, use the app-layer `order_service.memory_leak_oom`, where the app's
  own heap is the largest allocation and so the app is what dies.
- **Recovery is idempotent and best-effort.** Clearing a qdisc, killing tracked
  PIDs and removing the fill file are all no-ops on a clean pod, so recovering
  something already healthy is not an error.

## Verified on the live cluster

- Layer gating across all three modes for an application-only, a hybrid and an
  infrastructure-only failure, including the not-ok-when-nothing-ran case.
- Outcome aggregation, all six combinations: unavailable-with-sibling → ok,
  genuine-error → not ok, both-fine → ok, infra-only-unavailable → not ok,
  and both wrong-mode cases → not ok.
- Interface discovery via sysfs (returns `eth0`; no `ip` binary required).
- Preflight correctly blocks `tc` on all three services and passes `python3`/`dd`.
- CPU burn end to end: inject → 1 tracked process alive → `stop_stress` → 0 alive,
  pod still ready with `restartCount` unchanged → second recover does not raise.
- `user_service.high_latency` injected and recovered for real in default `hybrid`
  mode: env var set then returned to `0`, `tc` half reported `unavailable` with
  its remedy, overall `ok` with `degraded` set.
- `payment_service.high_cpu` infra half at 85% for 45s — spanning three liveness
  periods — with the pod still ready and `restartCount` unchanged at 0.
- `user_service.pool_exhaustion` end to end: MySQL `Threads_connected` 2 → 152
  against `max_connections=151`, back to 2 after recovery, user-service not
  restarted.
- `payment_service.dns_failure`: `getaddrinfo('redis')` resolved → failed after
  inject → resolved again once recovery restarted the pod.
- `order_service.memory_exhaust`: cgroup memory 81 → 255 MiB against the 256Mi
  limit, back to 46 MiB after recovery.
- `payment_service.disk_full`: 256 MiB file created and `/tmp` free fell by
  exactly 256 MB, both reversed on recovery.
- `order_service.http_500` infrastructure half: payment pod replaced on inject,
  Ready again after recovery.

Every failure that is not `tc`-blocked has now been exercised against the
cluster, and each run left no residue — no pidfile, no fill file, MySQL back to
its baseline connection count.
