---
title: order-service — external memory pressure (release the hog)
service: order-service
severity: sev1
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- memory
- exhaustion
- cgroup
- external
- resource
applicability:
  environments:
  - demo
  - production
  failure_category: resource_saturation_memory
  alerts:
  - EcommerceOrderServiceMemoryHigh
  required_signals:
  - memory_saturation
  allowed_services:
  - order-service
  allowed_namespaces:
  - ecommerce
prerequisites:
- id: incident_active
  description: The incident is still open and within the configured max age.
  mandatory: true
  check: incident_active
- id: target_in_scope
  description: Every step targets a service/namespace this runbook declares.
  mandatory: true
  check: service_scope
- id: alert_firing
  description: EcommerceOrderServiceMemoryHigh is still firing (advisory — skipped when Prometheus is unreachable).
  mandatory: false
  check: alert_firing
- id: signal_memory_saturation
  description: The memory_saturation signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: memory_saturation
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/order_service.memory_exhaust
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
---
# order-service — external memory pressure (release the hog)

| | |
|---|---|
| **Alert** | `EcommerceOrderServiceMemoryHigh` |
| **Service** | `order-service` |
| **Severity** | `sev1` |

## 1. Symptoms

`container_memory_working_set_bytes` for order-service pinned near its 256Mi limit; the application's own heap metrics look normal; restartCount is usually NOT incrementing.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the working set actually pinned at the limit?**

```powershell
max by (pod) (container_memory_working_set_bytes{namespace="ecommerce",pod=~"order-service-.*"} / (container_spec_memory_limit_bytes{namespace="ecommerce",pod=~"order-service-.*"} > 0))
```

Expect: Above 0.9. This is the rule's own expression, so it agrees with the alert.

**Is the APPLICATION leaking, or is the pressure external?**

```powershell
kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_MEMORY_LEAK")].value}'
```

Expect: Empty or false. `true` means the application heap leak - use order-service-memory-leak, which is a different fix.

**Was the container itself OOMKilled?**

```powershell
kubectl -n ecommerce get pods -l app=order-service -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}'
```

Expect: Usually EMPTY for this fault - the hog dies, not the app. An OOMKilled container with a climbing restartCount means use the -recycle runbook.

## 3. Root cause

An external process holds ~200MB resident inside the container's cgroup. The application is a bystander — its heap is clean, and the pressure comes from a neighbour in the same cgroup. The kernel reclaims rather than killing, and when it does kill it picks the hog (largest RSS), so pod-level `lastState.terminated.reason` is often NOT OOMKilled.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Kill the external process holding pages resident in the container's cgroup. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover order_service.memory_exhaust
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover order_service.memory_exhaust
```

Expect: The cgroup's working set falls away from the limit. If the kernel already OOMKilled the container, this is a no-op — the kernel got there first.

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

**Working set falls back below the threshold**

```
max by (pod) (container_memory_working_set_bytes{namespace="ecommerce",pod=~"order-service-.*"} / (container_spec_memory_limit_bytes{namespace="ecommerce",pod=~"order-service-.*"} > 0))
```

Expect: Comfortably under 0.9.

**The pod was never restarted by this remediation**

```
kubectl -n ecommerce get pods -l app=order-service
```

Expect: 1/1 Running with RESTARTS unchanged from before the fix.

## 6. If that did not fix it

- If the container was already OOMKilled, killing the hog is a no-op and the pod needs a clean start - use order-service-memory-exhaust-recycle.
- Do NOT raise the 256Mi limit. The pressure is external and unbounded, so a bigger limit only lengthens the interval before it is hit again.
- An OOMKilled container plus INJECT_MEMORY_LEAK=true is the application leak, not this fault. Read the env var before choosing.
- The injected hog self-expires after 600s, so a fault left alone appears to 'fix itself' - do not read that as a successful remediation.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.

## Notes

- This is NOT order-service-memory-leak. There the application grows its own heap (INJECT_MEMORY_LEAK=true) and the app is what gets OOMKilled; here the app is innocent and the RCA has to come from pod state rather than application logs.
- Both faults raise the same alert, because working-set-over-limit cannot tell you whose memory it is. The diagnose step below is what separates them.
- Recovery kills only the hog process. If the cgroup already OOMKilled the container, clearing is a no-op — the kernel got there first — and you want order-service-memory-exhaust-recycle instead.
