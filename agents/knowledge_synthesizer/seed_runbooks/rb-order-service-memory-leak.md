---
id: rb-order-service-memory-leak
title: order-service — memory leak leading to OOMKilled
service: order-service
version: 1
tags: [memory, oom, leak, resource, restart]
severity: Sev-1
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-08-03
---

## Symptoms
- Container RSS climbing steadily with order volume.
- Pod terminated with reason `OOMKilled`; restartCount incrementing.
- Brief scrape gaps and 5xx bursts during each restart.

## Affected service & blast radius
`order-service`. Blast radius of the fix: **LOW — clearing one env toggle; the pod rolls automatically.**

## Diagnosis
1. `kubectl -n ecommerce describe pod` shows lastState.terminated.reason=OOMKilled.
2. Memory growth correlates with order throughput, not with uptime — that points at a per-request leak rather than a slow accumulation.
3. `INJECT_MEMORY_LEAK=true` is set; faults.py appends a 5 MB chunk per order to a module-global list that is never freed.

## Resolution steps
1. **[clear_fault · low]** Set `INJECT_MEMORY_LEAK=false` and restore the 256Mi limit.
2. **[healthcheck · none]** Confirm RSS is flat under sustained order traffic.

## Verification
- RSS stays flat while orders continue.
- restartCount stops incrementing; `up` stays at 1.

## Rollback
1. Re-enable `INJECT_MEMORY_LEAK` to reproduce.

## Known wrong fixes (do NOT do these)
- Raise the memory limit — this delays the OOM instead of fixing it. The leak is unbounded, so a larger limit only means a longer interval between kills.
- Restart the pod on a schedule — frees the leaked memory but the leak resumes immediately. This is treating the symptom.
- Scale out to more replicas — every replica leaks at the same per-order rate.

## References
- Scenario: `order_service_memory_leak`
- Failure key: `order_service.memory_leak_oom`
- Alert rule: `EcommerceServiceDown`
- Truth file: `demo/ecommerce/truth_files/order_service_memory_leak.json`
