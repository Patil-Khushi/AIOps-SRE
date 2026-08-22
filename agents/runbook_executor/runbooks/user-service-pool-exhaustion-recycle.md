---
title: user-service — MySQL connection pool exhausted (release, then recycle pods)
service: user-service
severity: sev1
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- database
- connections
- pool
- recycle
- stale
applicability:
  environments:
  - demo
  - production
  failure_category: application_error
  alerts:
  - EcommerceUserLoginFailures
  required_signals:
  - error_rate_high
  allowed_services:
  - mysql
  - user-service
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
  description: EcommerceUserLoginFailures is still firing (advisory — skipped when Prometheus is unreachable).
  mandatory: false
  check: alert_firing
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
  target: fault/user_service.pool_exhaustion
  namespace: ecommerce
- name: verify-datastore-accepts-connections
  action: healthcheck
  destructive: false
  idempotent: true
  target: statefulset/mysql
  namespace: ecommerce
- name: drain-connections
  action: drain
  destructive: false
  idempotent: true
  target: deployment/user-service
  namespace: ecommerce
- name: restart-pods
  action: restart_deployment
  destructive: true
  idempotent: true
  rollback_action: rescale_previous
  target: deployment/user-service
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/user-service
  namespace: ecommerce
---
# user-service — MySQL connection pool exhausted (release, then recycle pods)

| | |
|---|---|
| **Alert** | `EcommerceUserLoginFailures` |
| **Service** | `user-service` |
| **Severity** | `sev1` |

## 1. Symptoms

`login_failure_total{reason="db_error"}` still climbing AFTER the held sessions were released; MySQL READY 1/1 and accepting connections, but /login keeps returning 500.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Have the held sessions actually been released yet?**

```powershell
uv run --no-project python -m failure_injection list
```

Expect: user_service.pool_exhaustion NOT listed as active. If it is still active, run user-service-pool-exhaustion first - recycling now achieves nothing.

**Is MySQL healthy but the app still failing?**

```powershell
kubectl -n ecommerce get statefulset mysql ; curl http://localhost:30081/health
```

Expect: mysql READY 1/1 while /health reports {"status":"degraded","mysql":false} - the split that means the app's own pool is stale.

## 3. Root cause

Same root cause as user-service-pool-exhaustion — an external client exhausted MySQL's 151 `max_connections`. The residual symptom is secondary: user-service's SQLAlchemy pool is holding connections that are open locally and already dead server-side, and it only discovers that on the next checkout.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Release the externally held MySQL sessions. This is the root-cause fix, and it does NOT touch user-service — the app was never the broken party.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover user_service.pool_exhaustion
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover user_service.pool_exhaustion
```

Expect: MySQL stops refusing new connections; user-service can open sessions again without being restarted.

### Step 2. verify-datastore-accepts-connections

`healthcheck` &middot; read-only

Confirm the server has headroom again BEFORE recycling. Restarting into a still-exhausted server just moves the failure to the new pods.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce get statefulset mysql
```

Expect: mysql READY 1/1.

### Step 3. drain-connections

`drain` &middot; read-only

Stop sending new traffic to the pods before they are replaced.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce annotate pod -l app=user-service drain=true --overwrite
```

Expect: In-flight requests finish; no new work is routed to the old pods.

### Step 4. restart-pods

`restart_deployment` &middot; **destructive - needs approval**

Roll the deployment. Requires approval; auto-rolls back if the new pods do not become healthy.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout restart deploy/user-service
kubectl -n ecommerce rollout status deploy/user-service --timeout=120s
```

Manual equivalent (Docker Compose):

```powershell
docker compose restart user-service
```

Expect: New pods reach Ready; restartCount on the old pods stops mattering.

Rollback if it fails: `rescale_previous`

### Step 5. verify-health

`healthcheck` &middot; read-only

Read-only check that the service recovered.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status deploy/user-service --timeout=120s
curl http://localhost:30081/health
```

Manual equivalent (Docker Compose):

```powershell
curl http://localhost:8001/health
```

Expect: HTTP 200 with {"status":"ok"} and every dependency true.

## 5. Verify the fix

**Database-backed login failures stop**

```
sum(rate(login_failure_total{reason="db_error"}[2m]))
```

Expect: 0.

**The service reports every dependency healthy**

```
curl http://localhost:30081/health
```

Expect: {"status":"ok","mysql":true}

## 6. If that did not fix it

- If the recycle brings the pods back and they fail again within seconds, the sessions were never released - re-check `failure_injection list`.
- A restart is not a fix for an exhausted server. It only helps the secondary symptom (a stale local pool), which is why this runbook clears the fault first.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.

## Notes

- Prefer user-service-pool-exhaustion first: it fixes the root cause and touches no workload. Reach for this one only when logins still fail after the sessions were released.
- The restart is gated and reversible (`rescale_previous`), so an unhealthy rollout is undone rather than left in place.
- Recycling WITHOUT clearing the fault first is useless — the new pods meet the same exhausted server. That is why clear_fault is step 1 here, not the restart.
