---
title: order-service — payment call timing out
service: order-service
severity: sev2
tags:
- timeout
- dependency
- payment
- gateway
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/order_service.payment_timeout
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
  target: deployment/order-service
  namespace: ecommerce
---
# order-service — payment call timing out

| | |
|---|---|
| **Alert** | `EcommercePaymentTimeouts` |
| **Service** | `order-service` |
| **Severity** | `sev2` |

## 1. Symptoms

`payment_timeout_total` climbing; POST /orders returns 504; orders left in FAILED state with a row still present in Postgres.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the gateway delay set above the client timeout?**

```powershell
kubectl -n ecommerce get deploy mock-payment-gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_DELAY_SECONDS")].value}'
```

Expect: A value above PAYMENT_TIMEOUT_SECONDS (default 5).

**Are timeouts counting up?**

```powershell
payment_timeout_total
```

Expect: Climbing.

**Confirm the fault is NOT on order-service or payment-service**

```powershell
kubectl -n ecommerce get pods -l app=payment-service
```

Expect: Running and healthy - it is blocked on its upstream, not broken.

## 3. Root cause

The mock payment gateway has `INJECT_DELAY_SECONDS` set above order-service's `PAYMENT_TIMEOUT_SECONDS` (default 5s), so every charge exceeds the client timeout.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover order_service.payment_timeout
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover order_service.payment_timeout
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
kubectl -n ecommerce rollout status deploy/order-service --timeout=120s
curl http://localhost:30082/health
```

Manual equivalent (Docker Compose):

```powershell
curl http://localhost:8002/health
```

Expect: HTTP 200 with {"status":"ok"} and every dependency true.

## 5. Verify the fix

**Timeout counter stops climbing**

```
rate(payment_timeout_total[2m])
```

Expect: 0

**An order completes and is PAID**

```
curl http://localhost:30082/health
```

Expect: {"status":"ok","postgres":true} and a new order returns status PAID.

## 6. If that did not fix it

- The fault is on mock-payment-gateway, two hops downstream. The alert names the victim, not the cause - restarting order-service or payment-service changes nothing.
- Do NOT raise PAYMENT_TIMEOUT_SECONDS to 'fix' it: that masks a broken dependency by making customers wait longer for the same failure.
- Orders that already failed stay FAILED. The row is kept deliberately so the customer can see the attempt.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- The fault is on mock-payment-gateway, NOT on order-service — the alert names the victim, not the cause. Same underlying fault as payment_service.gateway_timeout.
- Orders that already failed stay FAILED; the row is kept deliberately so the customer can see the attempt.
