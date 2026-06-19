---
title: Recover ad service from error spikes (adFailure)
service: ad
severity: sev2
tags:
- error
- 5xx
- errors
- flag
- crash
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/adFailure
  namespace: otel-demo
- name: verify-error-rate
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/ad
  namespace: otel-demo
---
# Recover ad service from error spikes

Use when `ad` is returning 5xx errors because the `adFailure` feature flag is
forcing failures.

1. **Reset feature flag** — flip `adFailure` back to `off` (safe, reversible).
   This is the root-cause fix.
2. **Verify error rate** — confirm the error spans have cleared (read-only).
   approval and rolls back to the previous scale if it does not come up healthy.
