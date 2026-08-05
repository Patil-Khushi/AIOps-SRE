---
title: user-service — high login latency
service: user-service
severity: sev2
tags:
- latency
- slow
- login
- p95
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/user_service.high_latency
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/user-service
  namespace: ecommerce
---
# user-service — high login latency

| | |
|---|---|
| **Alert** | `EcommerceOrderLatencyHigh` |
| **Service** | `user-service` |
| **Severity** | `sev2` |

## 1. Symptoms

p95 of `order_latency_seconds` above 2s; /login is slow but returns 200.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the latency toggle set?**

```powershell
kubectl -n ecommerce get deploy user-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_LATENCY_SECONDS")].value}'
```

Expect: Non-zero (healthy default is 0).

**Is it latency rather than errors?**

```powershell
histogram_quantile(0.95, sum by (le) (rate(order_latency_seconds_bucket[2m])))
```

Expect: Above 2s while orders still return 201.

## 3. Root cause

`INJECT_LATENCY_SECONDS` is set on the user-service Deployment, adding a fixed sleep to /login. Because order-service validates every order against /profile, the latency surfaces on the ORDER path too — which is why the alert that fires is EcommerceOrderLatencyHigh rather than a user-service alert.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover user_service.high_latency
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover user_service.high_latency
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

**p95 drops back under threshold**

```
histogram_quantile(0.95, sum by (le) (rate(order_latency_seconds_bucket[2m])))
```

Expect: Below 2s.

## 6. If that did not fix it

- The same alert fires for user_service.high_cpu and payment_service.high_cpu. Check which toggle is actually set before assuming latency injection.
- The latency is on /login, but order-service validates every order against /profile - which is why it shows up on the ORDER latency histogram.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- This alert can also be caused by user_service.high_cpu or payment_service.high_cpu — check which fault is actually active before assuming latency injection.
