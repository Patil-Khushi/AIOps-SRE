---
title: user-service — CPU saturation
service: user-service
severity: sev2
tags:
- cpu
- saturation
- latency
- throttling
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/user_service.high_cpu
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/user-service
  namespace: ecommerce
---
# user-service — CPU saturation

| | |
|---|---|
| **Alert** | `EcommerceOrderLatencyHigh` |
| **Service** | `user-service` |
| **Severity** | `sev2` |

## 1. Symptoms

Container CPU pinned at its limit; request latency climbs across every user-service endpoint.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the CPU toggle on?**

```powershell
kubectl -n ecommerce get deploy user-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_CPU_LOAD")].value}'
```

Expect: true

**Is the pod throttled rather than crashing?**

```powershell
kubectl -n ecommerce top pod -l app=user-service
```

Expect: CPU pinned near the 1000m limit; pod stays Running.

## 3. Root cause

`INJECT_CPU_LOAD=true` runs a busy loop in the request path. The pod is CPU-throttled against its 1000m limit rather than crashing, so it degrades instead of failing over.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover user_service.high_cpu
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover user_service.high_cpu
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. verify-health

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

**CPU returns to idle**

```
kubectl -n ecommerce top pod -l app=user-service
```

Expect: CPU well below the limit.

## 6. If that did not fix it

- A restart clears the symptom but the toggle is read per-request - without clearing it the busy loop returns immediately.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- Restarting the pod also clears the symptom, but the toggle is read from the environment at request time — a restart without clearing the fault brings the busy loop straight back.
