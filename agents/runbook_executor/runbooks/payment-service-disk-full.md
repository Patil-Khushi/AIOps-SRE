---
title: payment-service — disk pressure from a large file
service: payment-service
severity: sev2
version: 1
status: active
owner: sre-platform
approved_by: aiops-sre-review
tags:
- disk
- storage
- filesystem
- enospc
applicability:
  environments:
  - demo
  - production
  failure_category: resource_saturation_disk
  required_signals:
  - disk_saturation
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
- id: signal_disk_saturation
  description: The disk_saturation signal is present on the incident (advisory).
  mandatory: false
  check: signal_present
  signal: disk_saturation
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/payment_service.disk_full
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/payment-service
  namespace: ecommerce
---
# payment-service — disk pressure from a large file

| | |
|---|---|
| **Alert** | _none — this fault raises no Prometheus alert (see §2)_ |
| **Service** | `payment-service` |
| **Severity** | `sev2` |

## 1. Symptoms

A large file present under `/tmp` on the payment-service pod; application write paths failing with ENOSPC if the filesystem is genuinely full. **No alert fires for this fault**, so it is found by looking, not by being paged.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the fill file present? (this is the actual signal)**

```powershell
kubectl -n ecommerce exec deploy/payment-service -- ls -lh /tmp
```

Expect: A file of roughly 256MB. Its absence means this fault is not active.

**How much space does the filesystem report?**

```powershell
kubectl -n ecommerce exec deploy/payment-service -- df -h /tmp
```

Expect: Barely moved. /tmp is the node overlay (~1TB), so 256MB is ~0.025% - do NOT conclude from a healthy df that there is no fill file.

**Are writes actually failing?**

```powershell
kubectl -n ecommerce logs deploy/payment-service --tail=30
```

Expect: ENOSPC / 'No space left on device' on a write path. On this cluster there is usually plenty of headroom, so this is often clean even with the fault active.

## 3. Root cause

A 256MB file was written to `/tmp`, which on this cluster is the containerd overlay — the node's ~1TB filesystem, shared with etcd and every other pod. The write is real; the percentage it moves is not measurable.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Delete the injected fill file. Scoped to exactly that file — nothing else under /tmp is touched, because on this cluster /tmp is the node's shared overlay.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover payment_service.disk_full
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover payment_service.disk_full
```

Expect: The file is gone and no workload is restarted.

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

**The fill file is gone**

```
kubectl -n ecommerce exec deploy/payment-service -- ls -lh /tmp
```

Expect: No large file remains.

**Charges succeed**

```
curl http://localhost:30083/health
```

Expect: {"status":"ok","redis":true} and a new order reaches PAID.

## 6. If that did not fix it

- Do not wait for an alert to clear: none exists for this fault by design. The file's absence is the verification.
- If df shows the node filesystem genuinely near full, that is a CLUSTER problem, not this scenario - a 256MB scenario file cannot cause it. Investigate node disk usage before deleting anything else.
- Never widen the cleanup beyond the injected file. /tmp on this pod is the node's shared overlay, so deleting unfamiliar paths there can affect other workloads.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.

## Notes

- There is deliberately NO Prometheus rule for this fault. Absence of a disk alert is not evidence the fault is absent — check the filesystem directly.
- A percentage-based fill was rejected on purpose: `/` here is the node's filesystem, so filling it to 95% would be a cluster-wide outage rather than a service-level scenario.
- Making this produce a real DiskPressure signal needs an emptyDir with a sizeLimit mounted into the pod — see demo/ecommerce/k8s/20-app.yaml.
