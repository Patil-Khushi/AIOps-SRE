---
title: Reset payment feature flag (paymentFailure)
service: payment
severity: sev1
tags:
- error
- 5xx
- charge
- flag
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/paymentFailure
  namespace: otel-demo
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/payment
  namespace: otel-demo
---
# Reset payment feature flag (paymentFailure)

Payment is erroring because the `paymentFailure` flag is forcing failed charges.

1. **Reset feature flag** — flip `paymentFailure` back to `off` (safe, reversible). The root-cause fix.
2. **Verify health** — confirm the service recovered (read-only).
