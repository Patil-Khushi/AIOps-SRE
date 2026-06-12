---
id: rb-cart-failure
title: Cart service — 100% error rate (HTTP 5xx)
service: cart
version: 1
tags: [error-rate, flagd, feature-flag, cart, valkey]
severity: Sev-1
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-06-11
---

## Symptoms
- `CartErrorRateHigh` alert firing.
- Cart span p95 returns 500 across GetCart / AddItem / EmptyCart.
- Cart error rate at 100%. Customer impact: shopping cart cannot be opened or modified.

## Affected service & blast radius
`cart`. Blast radius of the fix: **LOW** — single feature flag, instant rollback.

## Diagnosis
1. Inspect cart operation spans — `STATUS_CODE_ERROR` across all cart operations.
2. Read flagd `flagd-config`: `cartFailure == on` returns 5xx on every cart request.
3. Rule out the valkey-cart backing store being unreachable and a deploy mid-rollback.

## Resolution steps
1. **[set_flag · low]** Flip `cartFailure` to `off` in the flagd ConfigMap (via the feature-flags seam).
2. **[manual · medium]** If cart errors persist after the flip, check valkey-cart pod health: `kubectl -n otel-demo logs deploy/valkey-cart`.

## Verification
- Cart error rate drops to ~0% within ~60s of the flag flip.
- `CartErrorRateHigh` clears; cart opens and updates normally.

## Rollback
1. Flip `cartFailure` back to its previous variant in the flagd ConfigMap.
2. `kubectl rollout undo` for the cart deployment.

## Known wrong fixes (do NOT do these)
- Restart valkey-cart — Valkey is healthy; restarting it only breaks active sessions.

## References
- Scenario: `cart_failure`
- flagd flag: `cartFailure`
