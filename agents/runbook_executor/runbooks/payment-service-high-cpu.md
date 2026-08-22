---
title: payment-service — CPU saturation
service: payment-service
severity: sev2
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- cpu
- saturation
- latency
- throttling
applicability:
  environments:
  - demo
  - production
  failure_category: resource_saturation_cpu
  alerts:
  - EcommercePaymentServiceCPUHigh
  required_signals:
  - cpu_saturation
  - latency_high
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
  description: EcommercePaymentServiceCPUHigh is still firing (advisory — skipped when Prometheus is unreachable).
  mandatory: false
  check: alert_firing
- id: signal_cpu_saturation
  description: The cpu_saturation signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: cpu_saturation
- id: signal_latency_high
  description: The latency_high signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: latency_high
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/payment_service.high_cpu
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/payment-service
  namespace: ecommerce
---
# payment-service — CPU saturation

| | |
|---|---|
| **Alert** | `EcommercePaymentServiceCPUHigh` |
| **Service** | `payment-service` |
| **Severity** | `sev2` |

## 1. Symptoms

payment-service CPU at its limit; charge latency climbing; order p95 crosses 2s because orders block on payment.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the CPU toggle on?**

```powershell
kubectl -n ecommerce get deploy payment-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="INJECT_CPU_LOAD")].value}'
```

Expect: true

## 3. Root cause

`INJECT_CPU_LOAD=true` runs a busy loop in the charge path; the pod is throttled against its 1000m limit.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover payment_service.high_cpu
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover payment_service.high_cpu
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

**CPU returns to idle**

```
kubectl -n ecommerce top pod -l app=payment-service
```

Expect: CPU well below the limit.

## 6. If that did not fix it

- Orders block on payment, so order p95 recovers only after payment-service does.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.
