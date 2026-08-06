---
title: order-service — 5xx on order creation
service: order-service
severity: sev2
tags:
- error
- 5xx
- orders
- application
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/order_service.http_500
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
---
# order-service — 5xx on order creation

| | |
|---|---|
| **Alert** | `EcommerceOrderErrorRateHigh` |
| **Service** | `order-service` |
| **Severity** | `sev2` |

## 1. Symptoms

`orders_failed_total{reason="injected_500"}` climbing; every POST /orders returns 500 immediately, before any dependency is called.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the 500 toggle on?**

```powershell
kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_HTTP_500")].value}'
```

Expect: true

**Which failure reason is counting up?**

```powershell
sum by (reason) (orders_failed_total)
```

Expect: reason="injected_500" climbing. A different reason means a different fault.

**Does it fail before touching any dependency?**

```powershell
kubectl -n ecommerce logs deploy/order-service --tail=20
```

Expect: "injected HTTP 500 on order creation" with no database or payment log lines after it.

## 3. Root cause

`INJECT_HTTP_500=true` on the order-service Deployment forces an unhandled 5xx at the top of the create-order handler.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover order_service.http_500
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover order_service.http_500
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. verify-health

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

**Error rate returns to zero**

```
sum by (reason) (rate(orders_failed_total[2m]))
```

Expect: 0 for injected_500.

**A real order succeeds**

```
See demo/ecommerce/README.md for the register -> login -> order sequence
```

Expect: HTTP 201 with "status":"PAID".

## 6. If that did not fix it

- This alert needs SUSTAINED traffic to fire and to clear - it is a rate() over 2 minutes, so a short burst decays to zero before the rule evaluates. Drive load with `--load 90` when reproducing.
- The failure is the first thing in the handler, so user validation, the DB write and the payment call never run. There is no partial state to clean up.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- The failure is first in the handler, so user validation, the database write and the payment call never run. No partial state to clean up.
- This alert needs SUSTAINED traffic to fire: it is a rate() over a 2m window, so a short burst decays to zero before the rule evaluates.
