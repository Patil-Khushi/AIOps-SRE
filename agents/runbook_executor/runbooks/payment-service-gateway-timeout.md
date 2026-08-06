---
title: payment-service — external gateway timing out
service: payment-service
severity: sev2
tags:
- timeout
- gateway
- dependency
- external
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/payment_service.gateway_timeout
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/mock-payment-gateway
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/payment-service
  namespace: ecommerce
---
# payment-service — external gateway timing out

| | |
|---|---|
| **Alert** | `EcommercePaymentTimeouts` |
| **Service** | `payment-service` |
| **Severity** | `sev2` |

## 1. Symptoms

POST /payments hangs then fails; upstream order-service reports 504.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the gateway delay above payment-service's timeout?**

```powershell
kubectl -n ecommerce get deploy mock-payment-gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_DELAY_SECONDS")].value}'
```

Expect: A value above GATEWAY_TIMEOUT_SECONDS (default 5).

## 3. Root cause

`INJECT_DELAY_SECONDS` on mock-payment-gateway exceeds payment-service's `GATEWAY_TIMEOUT_SECONDS`, so the outbound charge call times out.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover payment_service.gateway_timeout
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover payment_service.gateway_timeout
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. verify-health

`healthcheck` &middot; read-only

Read-only check that the service recovered.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status deploy/mock-payment-gateway --timeout=120s
```

Expect: HTTP 200 with {"status":"ok"} and every dependency true.

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

**A charge completes within the timeout**

```
curl http://localhost:30083/health
```

Expect: HTTP 200 and a new order reaches PAID.

## 6. If that did not fix it

- Restarting payment-service changes nothing - the fault is on the gateway it calls.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- The fault is on the gateway, not on payment-service. Restarting payment-service changes nothing.
