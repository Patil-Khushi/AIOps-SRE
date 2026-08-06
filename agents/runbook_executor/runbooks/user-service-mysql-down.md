---
title: user-service — MySQL unavailable
service: user-service
severity: sev1
tags:
- database
- mysql
- dependency
- 5xx
- login
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/user_service.mysql_down
  namespace: ecommerce
- name: wait-for-datastore
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
# user-service — MySQL unavailable

| | |
|---|---|
| **Alert** | `EcommerceMySQLDown` |
| **Service** | `user-service` |
| **Severity** | `sev1` |

## 1. Symptoms

`mysql_connection_status == 0`; POST /login and /register return HTTP 500; logs show `database connection failed` / connection refused to mysql:3306.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the MySQL StatefulSet scaled down?**

```powershell
kubectl -n ecommerce get statefulset mysql
```

Expect: READY 0/1. If it reads 1/1, this is NOT the fault - check user-service-crashloop instead.

**Is user-service running (not crashlooping)?**

```powershell
kubectl -n ecommerce get pods -l app=user-service
```

Expect: Running with restartCount 0. A climbing restartCount means crashloop, a different runbook.

**What does the service itself say?**

```powershell
curl http://localhost:30081/health
```

Expect: {"status":"degraded","mysql":false} - HTTP 200 by design, so a DB outage does not restart the pod.

## 3. Root cause

The MySQL StatefulSet is scaled to zero, so the SQLAlchemy engine cannot open a connection. user-service stays up and keeps serving 500s — it does not crashloop, because /health returns 200 with status=degraded by design.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover user_service.mysql_down
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover user_service.mysql_down
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. wait-for-datastore

`healthcheck` &middot; read-only

Wait for MySQL to accept connections before checking the app. The app pod is NOT restarted by recovery, so it may still be serving errors from its stale connection pool for a few seconds.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status statefulset/mysql --timeout=180s
```

Manual equivalent (Docker Compose):

```powershell
docker compose ps mysql
```

Expect: 1/1 READY; the mysqladmin ping readiness probe passes.

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

**Connection gauge is back up (PromQL at http://localhost:9090)**

```
mysql_connection_status
```

Expect: 1

**End-to-end: register then log in**

```
curl -X POST http://localhost:30081/register -H "Content-Type: application/json" -d '{"name":"t","email":"t1@example.com","password":"hunter2pass"}'
```

Expect: HTTP 201 with an id.

## 6. If that did not fix it

- Check the PVC still binds: `kubectl -n ecommerce get pvc data-mysql-0` - it should be Bound.
- If MySQL is Running but the gauge stays 0, the credentials drifted. Compare MYSQL_USER/MYSQL_PASSWORD in the `ecommerce-secrets` Secret against what the StatefulSet was initialised with - the password is only applied on FIRST boot with an empty PVC.
- Tail the datastore: `kubectl -n ecommerce logs statefulset/mysql --tail=50`.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- Recovery scales the StatefulSet back to 1 and waits for the rollout, so the first login after this runbook should already succeed.
- The PVC is retained across a scale-to-zero, so no user data is lost.
