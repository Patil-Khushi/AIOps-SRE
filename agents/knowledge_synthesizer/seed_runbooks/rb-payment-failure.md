---
id: rb-payment-failure
title: Payment service — 100% Charge error rate (HTTP 500s)
service: payment
version: 1
tags: [error-rate, flagd, feature-flag, payment, grpc]
severity: Sev-1
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-06-11
---

## Symptoms
- `PaymentErrorRateHigh` alert firing.
- `rate(traces_span_metrics_calls_total{service_name="payment",status_code="STATUS_CODE_ERROR"}[2m]) > 0` — error on every Charge span.
- Payment error rate at 100% across all charges. Customer impact: checkout fails.

## Affected service & blast radius
`payment`. Blast radius of the fix: **LOW** — single feature flag, instant rollback.

## Diagnosis
1. Inspect Charge gRPC spans — `status_code=STATUS_CODE_ERROR` on every call.
2. Read flagd `flagd-config`: `paymentFailure` variant at `100%` deterministically errors every Charge.
3. Rule out downstream (Redis/DB) — the error originates in the application layer, not capacity or backing store.

## Resolution steps
1. **[set_flag · low]** Flip `paymentFailure` variant to `off` in the flagd ConfigMap (via the feature-flags seam).
2. **[restart · medium]** Restart payment pods only if the flag flip doesn't recover within 60s.

## Verification
- Charge error rate drops to ~0% within ~60s of the flag flip.
- `PaymentErrorRateHigh` clears; checkout succeeds.

## Rollback
1. Flip `paymentFailure` back to its previous variant in the flagd ConfigMap.
2. `kubectl rollout undo` for the payment deployment.

## Known wrong fixes (do NOT do these)
- Scale up payment pods — capacity is not the bottleneck; every request still fails 100%.
- Restart the database — the DB is healthy; the error is in the application layer.

## References
- Scenario: `payment_failure`
- flagd flag: `paymentFailure`
