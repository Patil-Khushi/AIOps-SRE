---
id: rb-payment-service-redis-down
title: payment-service — Redis unavailable, charges rejected
service: payment-service
version: 1
tags: [redis, datastore, dependency, payments, 5xx]
severity: Sev-1
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-08-03
---

## Symptoms
- `redis_connection_status == 0`; POST /payments returns 500.
- Logs: `redis connection error`.
- Orders reach PENDING then FAILED with reason `payment_failed`.

## Affected service & blast radius
`payment-service`. Blast radius of the fix: **LOW — scaling one StatefulSet back up; the PVC is retained.**

## Diagnosis
1. `kubectl -n ecommerce get statefulset redis` shows replicas at 0.
2. Redis here is the payment RECORD STORE, not a cache — there is no fallback path, so the charge is rejected rather than silently unrecorded.
3. payment-service pods stay Running: socket timeouts are capped at 2s so a dead Redis fails fast instead of hanging the request.

## Resolution steps
1. **[clear_fault · low]** Scale the Redis StatefulSet back to 1.
2. **[healthcheck · none]** Confirm `redis_connection_status == 1` and a payment succeeds.

## Verification
- `redis_connection_status` returns to 1; `EcommerceRedisDown` clears.
- A full order reaches PAID and a `payment:*` key appears in Redis.

## Rollback
1. Scale Redis back to 0 to reproduce.

## Known wrong fixes (do NOT do these)
- Treat this as a cache miss and 'degrade gracefully' — Redis is the system of record for payments here. Skipping it would take money without recording it.
- Restart payment-service — the app is healthy; its datastore is gone.
- Delete the Redis PVC — destroys every historical payment record.

## References
- Scenario: `payment_service_redis_down`
- Failure key: `payment_service.redis_down`
- Alert rule: `EcommerceRedisDown`
- Truth file: `demo/ecommerce/truth_files/payment_service_redis_down.json`
