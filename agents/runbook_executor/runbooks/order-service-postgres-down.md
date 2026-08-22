---
title: order-service — PostgreSQL unavailable
service: order-service
severity: sev1
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- database
- postgres
- dependency
- 5xx
- orders
applicability:
  environments:
  - demo
  - production
  failure_category: dependency_unavailable
  alerts:
  - EcommercePostgresDown
  required_signals:
  - dependency_unavailable
  - error_rate_high
  allowed_services:
  - order-service
  - postgres
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
  description: EcommercePostgresDown is still firing (advisory — skipped when Prometheus is unreachable).
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
  target: fault/order_service.postgres_down
  namespace: ecommerce
- name: wait-for-datastore
  action: healthcheck
  destructive: false
  idempotent: true
  target: statefulset/postgres
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
---
# order-service — PostgreSQL unavailable

| | |
|---|---|
| **Alert** | `EcommercePostgresDown` |
| **Service** | `order-service` |
| **Severity** | `sev1` |

## 1. Symptoms

`postgres_connection_status == 0`; POST /orders returns 500; `orders_failed_total{reason="db_error"}` climbing.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is Postgres scaled down?**

```powershell
kubectl -n ecommerce get statefulset postgres
```

Expect: READY 0/1.

**What does order-service report?**

```powershell
curl http://localhost:30082/health
```

Expect: {"status":"degraded","postgres":false}

**Which failure reason?**

```powershell
sum by (reason) (orders_failed_total)
```

Expect: reason="db_error" climbing.

## 3. Root cause

The Postgres StatefulSet is scaled to zero. Orders fail at the persist step, before payment is ever called — so no charge is attempted and no money moves.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover order_service.postgres_down
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover order_service.postgres_down
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. wait-for-datastore

`healthcheck` &middot; read-only

Wait for PostgreSQL to accept connections before checking the app. The app pod is NOT restarted by recovery, so it may still be serving errors from its stale connection pool for a few seconds.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status statefulset/postgres --timeout=180s
```

Manual equivalent (Docker Compose):

```powershell
docker compose ps postgres
```

Expect: 1/1 READY; the pg_isready readiness probe passes.

### Step 3. verify-health

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

**Connection gauge recovers**

```
postgres_connection_status
```

Expect: 1

**An order persists and reaches PAID**

```
curl http://localhost:30082/health
```

Expect: {"status":"ok","postgres":true}

## 6. If that did not fix it

- The failure happens BEFORE the payment call, so there are no orphaned charges to reconcile.
- Check the PVC: `kubectl -n ecommerce get pvc data-postgres-0` should be Bound.
- If Postgres is Running but the gauge stays 0, check PGDATA - the official image refuses to initialise into a non-empty mount unless data lives in a subdirectory.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- Because the failure happens before the payment call, there are no orphaned charges to reconcile after recovery.
