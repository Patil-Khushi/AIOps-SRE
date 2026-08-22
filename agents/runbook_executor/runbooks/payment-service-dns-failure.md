---
title: payment-service — DNS resolution broken
service: payment-service
severity: sev1
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- dns
- resolution
- network
- gateway
- dependency
applicability:
  environments:
  - demo
  - production
  failure_category: dependency_unavailable
  alerts:
  - EcommercePaymentGatewayUnreachable
  - EcommerceRedisDown
  required_signals:
  - dependency_unavailable
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
  description: EcommercePaymentGatewayUnreachable is still firing (advisory — skipped when Prometheus is unreachable).
  mandatory: false
  check: alert_firing
- id: signal_dependency_unavailable
  description: The dependency_unavailable signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: dependency_unavailable
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/payment_service.dns_failure
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/payment-service
  namespace: ecommerce
---
# payment-service — DNS resolution broken

| | |
|---|---|
| **Alert** | `EcommercePaymentGatewayUnreachable` |
| **Service** | `payment-service` |
| **Severity** | `sev1` |

## 1. Symptoms

`payment_failures_total{reason="gateway_error"}` climbing; `getaddrinfo` failures in the logs. **`EcommerceRedisDown` also fires, with Redis perfectly healthy** — that pair is the fingerprint, not two separate incidents.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is Redis actually down, or just unresolvable? (do this FIRST)**

```powershell
kubectl -n ecommerce get statefulset redis
```

Expect: READY 1/1. Redis healthy while EcommerceRedisDown is firing IS the DNS fingerprint. A genuine 0/1 means payment-service-redis-down instead.

**Which payment failure reason is climbing?**

```powershell
sum by (reason) (payment_failures_total)
```

Expect: reason="gateway_error" - a connection or name-resolution failure. reason="gateway_timeout" is a different fault (payment-service-gateway-timeout).

**Can the pod resolve anything at all?**

```powershell
kubectl -n ecommerce exec deploy/payment-service -- getent hosts redis
```

Expect: No output and a non-zero exit. A healthy pod prints an IP. Also check `kubectl -n ecommerce exec deploy/payment-service -- cat /etc/resolv.conf`.

## 3. Root cause

`/etc/resolv.conf` on the payment-service pod is poisoned, so no name resolves — not the payment gateway, and not Redis either. payment-service re-pings Redis inside its own `/metrics` handler and the gauge zeroes on ANY exception, so a name-resolution failure drives `redis_connection_status` to 0 and raises a Redis alert that points at a healthy datastore.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Replace the pod so the kubelet writes a clean /etc/resolv.conf. There is no in-place repair: the file is generated at pod start, so a new pod is the only fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover payment_service.dns_failure
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover payment_service.dns_failure
```

Expect: A new pod reaches Ready with a working resolver; name lookups succeed and the misleading Redis alert clears on its own.

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

**Gateway connection failures stop**

```
sum(rate(payment_failures_total{reason="gateway_error"}[2m]))
```

Expect: 0.

**The misleading Redis signal clears too, without touching Redis**

```
redis_connection_status
```

Expect: 1 - because the name resolves again, not because Redis changed.

**The service reports every dependency healthy**

```
curl http://localhost:30083/health
```

Expect: {"status":"ok","redis":true}

## 6. If that did not fix it

- If EcommerceRedisDown persists AFTER the restart and Redis is still 1/1, re-check resolution from inside the new pod - a surviving alert with a healthy datastore is still a DNS symptom, not a Redis one.
- If Redis is genuinely 0/1, you are in payment-service-redis-down and this runbook does not apply.
- There is no in-place repair for resolv.conf: the kubelet writes it at pod start, so the pod must be replaced.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.

## Notes

- Recovery restarts the pod, because that is the only thing that restores resolv.conf — the kubelet writes it when the pod starts. There is no in-place fix.
- Do NOT act on the accompanying EcommerceRedisDown alert. Scaling or restarting Redis fixes nothing and takes a healthy datastore down for no reason.
- This is distinct from payment_service.gateway_timeout: there the gateway answers too slowly (reason=gateway_timeout), here it cannot be reached at all (reason=gateway_error).
