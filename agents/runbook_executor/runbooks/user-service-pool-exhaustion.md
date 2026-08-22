---
title: user-service — MySQL connection pool exhausted (release held sessions)
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
- saturation
- login
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
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/user-service
  namespace: ecommerce
---
# user-service — MySQL connection pool exhausted (release held sessions)

| | |
|---|---|
| **Alert** | `EcommerceUserLoginFailures` |
| **Service** | `user-service` |
| **Severity** | `sev1` |

## 1. Symptoms

`login_failure_total{reason="db_error"}` climbing; POST /login returns 500; `mysql_connection_status` flapping between 1 and 0. MySQL itself is Running and READY 1/1 the whole time.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is MySQL itself healthy? (this is what separates it from mysql_down)**

```powershell
kubectl -n ecommerce get statefulset mysql
```

Expect: READY 1/1. A 0/1 here means the StatefulSet is scaled down - that is user-service-mysql-down, not pool exhaustion.

**Which login failure reason is climbing?**

```powershell
sum by (reason) (login_failure_total)
```

Expect: reason="db_error" climbing. reason="invalid_credentials" alone is NOT a fault - every load generator posts a bogus password by design, which is exactly why the alert rule pins the reason.

**Does the app report a server-side refusal rather than its own pool filling?**

```powershell
kubectl -n ecommerce logs deploy/user-service --tail=30
```

Expect: 'Too many connections' raised on connect. A 'QueuePool limit ... overflow' message instead would mean the APP's pool is the bottleneck, not the server's.

## 3. Root cause

An external client holds ~155 sessions open against MySQL, whose `max_connections` is 151. The server refuses every NEW connection with 'Too many connections'. user-service is a bystander: its own SQLAlchemy pool is healthy, it simply cannot open anything new. This is the production shape of the failure — some other client exhausts the server and an innocent service starts failing.

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

Confirm MySQL is accepting new connections again. Unlike the mysql_down runbook this is NOT waiting for a rollout — MySQL never went down, it was only refusing new sessions.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce get statefulset mysql
kubectl -n ecommerce logs deploy/user-service --tail=20
```

Expect: mysql still READY 1/1, and no further 'Too many connections' lines appear in the user-service log.

### Step 3. verify-health

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

**Database-backed login failures stop (PromQL at http://localhost:9090)**

```
sum(rate(login_failure_total{reason="db_error"}[2m]))
```

Expect: 0.

**A fresh connection succeeds end to end**

```
curl -X POST http://localhost:30081/register -H "Content-Type: application/json" -d '{"name":"t","email":"pool1@example.com","password":"hunter2pass"}'
```

Expect: HTTP 201 with an id.

## 6. If that did not fix it

- Recovery does NOT restart user-service, so a pool still holding sockets that died server-side will keep failing. That is what user-service-pool-exhaustion-recycle is for.
- Do not raise MySQL's max_connections to 'fix' this. The external client exhausts whatever ceiling exists; the fix is to stop the client holding the sessions.
- This fault leaves MySQL READY 1/1 throughout. If MySQL is 0/1 you are in user-service-mysql-down and this runbook will not help.
- The injected holder self-expires after 600s, so a fault left alone appears to 'fix itself' - do not read that as a successful remediation.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.

## Notes

- Recovery releases the external sessions WITHOUT restarting user-service — which is the point: the app was never the problem, so it is not the thing to restart.
- If /login still fails once the sessions are released, the app's pool is holding dead sockets. Use user-service-pool-exhaustion-recycle for that.
- MySQL's own `max_connections` is not raised as part of recovery. Raising it would only move the ceiling — the external client would exhaust the new one too.
