---
id: rb-order-service-payment-timeout
title: order-service — payment calls timing out at the gateway
service: order-service
version: 1
tags: [timeout, dependency, payment, gateway]
severity: Sev-2
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-08-03
---

## Symptoms
- `payment_timeout_total` climbing; POST /orders returns HTTP 504.
- Orders persisted then marked FAILED; the row is retained deliberately.
- Traces show the order → payment span exceeding the client timeout.

## Affected service & blast radius
`order-service`. Blast radius of the fix: **LOW — clearing a delay toggle on the mock gateway.**

## Diagnosis
1. `INJECT_DELAY_SECONDS` on mock-payment-gateway exceeds order-service's `PAYMENT_TIMEOUT_SECONDS` (default 5s).
2. The fault is on the GATEWAY, two hops downstream. The alert names the victim, not the cause — a recurring trap in this topology.
3. payment-service itself is healthy; it is blocked waiting on its upstream.

## Resolution steps
1. **[clear_fault · low]** Reset `INJECT_DELAY_SECONDS` to 0 on mock-payment-gateway.
2. **[healthcheck · none]** Confirm a full order completes and reaches PAID.

## Verification
- `payment_timeout_total` stops climbing; `EcommercePaymentTimeouts` clears.
- POST /orders returns 201 with status PAID.

## Rollback
1. Set `INJECT_DELAY_SECONDS` back above the client timeout.

## Known wrong fixes (do NOT do these)
- Raise `PAYMENT_TIMEOUT_SECONDS` on order-service — this masks a broken dependency by making customers wait longer for the same failure.
- Restart order-service or payment-service — neither holds the fault.
- Scale up payment-service — it is idle, blocked on its upstream, not saturated.

## References
- Scenario: `order_service_payment_timeout`
- Failure key: `order_service.payment_timeout`
- Alert rule: `EcommercePaymentTimeouts`
- Truth file: `demo/ecommerce/truth_files/order_service_payment_timeout.json`
