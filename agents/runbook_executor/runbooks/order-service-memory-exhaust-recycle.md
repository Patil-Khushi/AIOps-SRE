---
title: order-service — external memory pressure (release, then recycle pods)
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
- recycle
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
- name: drain-connections
  action: drain
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
- name: restart-pods
  action: restart_deployment
  destructive: true
  idempotent: true
  rollback_action: rescale_previous
  target: deployment/order-service
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
---
# order-service — external memory pressure (release, then recycle pods)

| | |
|---|---|
| **Alert** | `EcommerceOrderServiceMemoryHigh` |
| **Service** | `order-service` |
| **Severity** | `sev1` |

## 1. Symptoms

Working set at the 256Mi limit AND the container has been OOMKilled — `lastState.terminated.reason=OOMKilled` with restartCount climbing, so the pod is cycling rather than merely under pressure.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the pod actually cycling (which is what justifies the restart)?**

```powershell
kubectl -n ecommerce get pods -l app=order-service
```

Expect: RESTARTS climbing. If it is stable at 1/1, use order-service-memory-exhaust instead and avoid the rollout.

**Was it OOMKilled rather than erroring?**

```powershell
kubectl -n ecommerce get pods -l app=order-service -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}'
```

Expect: OOMKilled. `Error` instead points at a startup failure, not memory.

**Is the pressure external rather than the application's own heap?**

```powershell
kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_MEMORY_LEAK")].value}'
```

Expect: Empty or false. `true` is the application leak - order-service-memory-leak.

## 3. Root cause

Same external memory pressure as order-service-memory-exhaust, but this time the cgroup OOM killer picked the container's main process rather than the hog. Releasing the hog stops the pressure; it does not bring a cycling pod back cleanly.

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

### Step 2. drain-connections

`drain` &middot; read-only

Stop sending new traffic to the pods before they are replaced.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce annotate pod -l app=order-service drain=true --overwrite
```

Expect: In-flight requests finish; no new work is routed to the old pods.

### Step 3. restart-pods

`restart_deployment` &middot; **destructive - needs approval**

Roll the deployment. Requires approval; auto-rolls back if the new pods do not become healthy.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout restart deploy/order-service
kubectl -n ecommerce rollout status deploy/order-service --timeout=120s
```

Manual equivalent (Docker Compose):

```powershell
docker compose restart order-service
```

Expect: New pods reach Ready; restartCount on the old pods stops mattering.

Rollback if it fails: `rescale_previous`

### Step 4. verify-health

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

**Restarts stop**

```
kubectl -n ecommerce get pods -l app=order-service
```

Expect: 1/1 Running and RESTARTS stops incrementing.

**Working set stays flat under sustained order traffic**

```
max by (pod) (container_memory_working_set_bytes{namespace="ecommerce",pod=~"order-service-.*"} / (container_spec_memory_limit_bytes{namespace="ecommerce",pod=~"order-service-.*"} > 0))
```

Expect: Level and well under 0.9.

## 6. If that did not fix it

- If the new pod is OOMKilled again within seconds, the hog was not released - check `failure_injection list` before restarting a third time.
- Do NOT raise the memory limit as a workaround; the external pressure is unbounded.
- Confirm the limit is back at its manifest value: `kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'` should be 256Mi.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.

## Notes

- Prefer order-service-memory-exhaust when the container is still Running: it is the same fix without a rollout.
- clear_fault comes FIRST. Restarting into live memory pressure just OOMKills the new pod too.
- The restart is gated and reversible (`rescale_previous`).
