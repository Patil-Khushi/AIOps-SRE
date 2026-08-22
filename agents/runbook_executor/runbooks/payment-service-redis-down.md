---
title: payment-service — Redis unavailable
service: payment-service
severity: sev1
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- cache
- redis
- dependency
- 5xx
- payments
applicability:
  environments:
  - demo
  - production
  failure_category: dependency_unavailable
  alerts:
  - EcommerceRedisDown
  required_signals:
  - dependency_unavailable
  - error_rate_high
  allowed_services:
  - payment-service
  - redis
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
  description: EcommerceRedisDown is still firing (advisory — skipped when Prometheus is unreachable).
  mandatory: false
  check: alert_firing
- id: signal_dependency_unavailable
  description: The dependency_unavailable signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: dependency_unavailable
- id: signal_error_rate_high
  description: The error_rate_high signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: error_rate_high
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/payment_service.redis_down
  namespace: ecommerce
- name: wait-for-datastore
  action: healthcheck
  destructive: false
  idempotent: true
  target: statefulset/redis
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/payment-service
  namespace: ecommerce
---
# payment-service — Redis unavailable

| | |
|---|---|
| **Alert** | `EcommerceRedisDown` |
| **Service** | `payment-service` |
| **Severity** | `sev1` |

## 1. Symptoms

`redis_connection_status == 0`; POST /payments returns 500; logs show `redis connection error`.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is Redis scaled down?**

```powershell
kubectl -n ecommerce get statefulset redis
```

Expect: READY 0/1.

**What does payment-service report?**

```powershell
curl http://localhost:30083/health
```

Expect: {"status":"degraded","redis":false}

**Are payments failing rather than hanging?**

```powershell
kubectl -n ecommerce logs deploy/payment-service --tail=20
```

Expect: "redis connection error" - socket timeouts are capped at 2s so a dead Redis fails fast.

## 3. Root cause

The Redis StatefulSet is scaled to zero. Redis is the payment record store here, not a cache — payments cannot be persisted, so the charge is rejected rather than silently unrecorded.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover payment_service.redis_down
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover payment_service.redis_down
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. wait-for-datastore

`healthcheck` &middot; read-only

Wait for Redis to accept connections before checking the app. The app pod is NOT restarted by recovery, so it may still be serving errors from its stale connection pool for a few seconds.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status statefulset/redis --timeout=180s
```

Manual equivalent (Docker Compose):

```powershell
docker compose ps redis
```

Expect: 1/1 READY; the redis-cli ping readiness probe passes.

### Step 3. verify-health

`healthcheck` &middot; read-only

Read-only check that the service recovered.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status deploy/payment-service --timeout=120s
curl http://localhost:30083/health
```

Manual equivalent (Docker Compose):

```powershell
curl http://localhost:8003/health
```

Expect: HTTP 200 with {"status":"ok"} and every dependency true.

## 5. Verify the fix

**Connection gauge recovers**

```
redis_connection_status
```

Expect: 1

**A payment record is written**

```
kubectl -n ecommerce exec statefulset/redis -- redis-cli KEYS 'payment:*'
```

Expect: At least one key after a successful order.

## 6. If that did not fix it

- Redis here is the payment SYSTEM OF RECORD, not a cache. Do not 'degrade gracefully' past it - that would take money without recording it.
- Payment records written before the outage survive: the PVC is retained across a scale-to-zero.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- Redis here is a system of record, not a cache — do NOT treat this as a cache-miss degradation. There is no fallback path.
- Payment records written before the outage survive: the PVC is retained.
