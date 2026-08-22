---
title: order-service — network packet loss
service: order-service
severity: sev2
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- network
- packet-loss
- tcp
- retransmits
applicability:
  environments:
  - demo
  - production
  failure_category: network_degradation
  required_signals:
  - packet_loss
  allowed_services:
  - order-service
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
- id: signal_packet_loss
  description: The packet_loss signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: packet_loss
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/order_service.packet_loss
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/order-service
  namespace: ecommerce
---
# order-service — network packet loss

| | |
|---|---|
| **Alert** | _none — this fault raises no Prometheus alert (see §2)_ |
| **Service** | `order-service` |
| **Severity** | `sev2` |

## 1. Symptoms

Order creation intermittently fails with connection errors under load; TCP retransmits climbing. No dedicated alert fires — 5% loss degrades rather than breaks, and it may surface only as raised latency on the existing order rules.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Can this fault even be applied on this cluster?**

```powershell
kubectl -n ecommerce exec deploy/order-service -- tc qdisc show dev eth0
```

Expect: A netem qdisc with `loss 5%`. If tc is 'not found' or the call is denied, the injector could never have applied it (no iproute2, no CAP_NET_ADMIN) - packet loss is NOT what you are looking at, so stop here.

**Are orders failing on the network rather than on a dependency?**

```powershell
sum by (reason) (orders_failed_total)
```

Expect: A connection/network reason climbing rather than reason="injected_500" or "db_error", both of which point at different runbooks.

## 3. Root cause

5% packet loss applied to the order-service pod's network interface with `tc netem`. Note this fault frequently CANNOT be injected on this cluster at all: the app images ship without `iproute2` and the pods do not hold CAP_NET_ADMIN, so the injector has nothing to drive.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Remove the netem qdisc from the pod's interface.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover order_service.packet_loss
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover order_service.packet_loss
```

Expect: The qdisc returns to the pod default and packets stop being dropped. A no-op if the loss was never applied — which on this cluster is the usual case.

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

**The qdisc is back to the pod default**

```
kubectl -n ecommerce exec deploy/order-service -- tc qdisc show dev eth0
```

Expect: No netem entry.

**Orders succeed consistently under load**

```
See demo/ecommerce/README.md for the register -> login -> order sequence
```

Expect: HTTP 201 with "status":"PAID", repeatably.

## 6. If that did not fix it

- On this cluster the injector usually cannot apply packet loss at all (no iproute2 in the image, no CAP_NET_ADMIN on the pod). If tc is unavailable, this fault is not active and you are chasing the wrong runbook.
- If you are chasing a REAL network problem rather than an injected one, this runbook does not apply - look at the CNI and node-level interface counters instead.
- 5% loss degrades rather than breaks. Expect intermittent failures and raised latency, not a clean outage.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.

## Notes

- There is deliberately no Prometheus rule for packet loss, so do not wait for an alert to clear as your signal that this is fixed.
- Verify the fault can even exist here before spending time on it — see the first diagnose step. On this cluster the usual answer is that it cannot.
