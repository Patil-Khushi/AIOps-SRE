---
title: payment-service — 5xx on charge
service: payment-service
severity: sev2
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- error
- 5xx
- payments
- application
applicability:
  environments:
  - demo
  - production
  failure_category: application_error
  alerts:
  - EcommerceOrderErrorRateHigh
  required_signals:
  - error_rate_high
  allowed_services:
  - payment-service
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
  description: EcommerceOrderErrorRateHigh is still firing (advisory — skipped when Prometheus is unreachable).
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
  target: fault/payment_service.http_500
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/payment-service
  namespace: ecommerce
---
# payment-service — 5xx on charge

| | |
|---|---|
| **Alert** | `EcommerceOrderErrorRateHigh` |
| **Service** | `payment-service` |
| **Severity** | `sev2` |

## 1. Symptoms

POST /payments returns 500; order-service marks orders FAILED with `orders_failed_total{reason="payment_failed"}`.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the 500 toggle on?**

```powershell
kubectl -n ecommerce get deploy payment-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_HTTP_500")].value}'
```

Expect: true

**How does it surface upstream?**

```powershell
sum by (reason) (orders_failed_total)
```

Expect: reason="payment_failed" climbing - order-service reports the downstream failure.

## 3. Root cause

`INJECT_HTTP_500=true` on the payment-service Deployment forces a 5xx on every charge.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover payment_service.http_500
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover payment_service.http_500
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. verify-health

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

**Charges succeed again**

```
curl http://localhost:30083/health
```

Expect: {"status":"ok","redis":true} and a new order reaches PAID.

## 6. If that did not fix it

- Orders reach PENDING then FAILED. The order row survives so the failed attempt stays visible to the customer.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- Orders reach PENDING and are then marked FAILED — the order row survives so the failed attempt stays visible to the customer.
