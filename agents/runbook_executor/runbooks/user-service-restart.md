---
title: user-service — restart (generic recovery)
service: user-service
severity: sev3
tags:
- restart
- generic
- unknown
steps:
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
# user-service — restart (generic recovery)

| | |
|---|---|
| **Alert** | `EcommerceServiceDown` |
| **Service** | `user-service` |
| **Severity** | `sev3` |

## 1. Symptoms

user-service degraded with no identified injected fault.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

```powershell
kubectl -n ecommerce get pods
```

## 3. Root cause

Unknown. Use when the specific fault runbooks do not match.

## 4. Procedure

### Step 1. drain-connections

`drain` &middot; read-only

Stop sending new traffic to the pods before they are replaced.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce annotate pod -l app=user-service drain=true --overwrite
```

Expect: In-flight requests finish; no new work is routed to the old pods.

### Step 2. restart-pods

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

- `EcommerceServiceDown` clears in Prometheus.

## Notes

- If the symptom returns after the restart, an injected fault is still set — check the scenario catalog before restarting again.
