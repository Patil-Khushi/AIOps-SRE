---
title: order-service — memory leak / OOMKilled
service: order-service
severity: sev1
tags:
- memory
- oom
- leak
- restart
- resource
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/order_service.memory_leak_oom
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
---
# order-service — memory leak / OOMKilled

| | |
|---|---|
| **Alert** | `EcommerceServiceDown` |
| **Service** | `order-service` |
| **Severity** | `sev1` |

## 1. Symptoms

RSS climbing with every order; pod terminated with reason OOMKilled; restartCount incrementing; scrapes fail during each restart.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Was the container OOM-killed?**

```powershell
kubectl -n ecommerce get pods -l app=order-service -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}'
```

Expect: OOMKilled. Anything else means a different failure.

**Is the leak toggle on?**

```powershell
kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_MEMORY_LEAK")].value}'
```

Expect: true

**Does memory track order volume rather than uptime?**

```powershell
container_memory_rss_bytes{namespace="ecommerce",pod=~"order-service.*"}
```

Expect: A sawtooth climbing with traffic and dropping at each restart - a per-request leak, not a slow accumulation.

## 3. Root cause

`INJECT_MEMORY_LEAK=true` appends a 5 MB chunk to a module-global list on every order and never frees it. The container hits its 256Mi limit and the kernel OOM-kills it.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover order_service.memory_leak_oom
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover order_service.memory_leak_oom
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. verify-health

`healthcheck` &middot; read-only

Read-only check that the service recovered.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status deploy/order-service --timeout=120s
curl http://localhost:30082/health
```

Manual equivalent (Docker Compose):

```powershell
curl http://localhost:8002/health
```

Expect: HTTP 200 with {"status":"ok"} and every dependency true.

## 5. Verify the fix

**RSS stays flat under sustained order traffic**

```
container_memory_rss_bytes{namespace="ecommerce",pod=~"order-service.*"}
```

Expect: Roughly level instead of climbing.

**No further restarts**

```
kubectl -n ecommerce get pods -l app=order-service
```

Expect: RESTARTS stops incrementing.

## 6. If that did not fix it

- Do NOT just raise the memory limit. The leak is unbounded, so a bigger limit only lengthens the interval between kills.
- A restart frees the leaked memory but the leak resumes immediately - clearing the toggle is the actual fix.
- Confirm the limit is back at its manifest value: `kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'` should be 256Mi.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- The 256Mi limit is deliberate — it makes the leak reach OOMKilled in demo-friendly time. Raising it hides the symptom rather than fixing it.
- Clearing the fault also resets the limit, because the injector lowers it as part of the fault.
- A restart frees the leaked memory but the leak resumes immediately; clearing the toggle is the actual fix.
