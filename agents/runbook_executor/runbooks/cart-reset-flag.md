---
title: Reset cart feature flag (cartFailure)
service: cart
severity: sev1
tags:
- error
- 5xx
- flag
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/cartFailure
  namespace: otel-demo
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/cart
  namespace: otel-demo
---
# Reset cart feature flag (cartFailure)

Cart is erroring because the `cartFailure` flag is forcing failures on every request.

1. **Reset feature flag** — flip `cartFailure` back to `off` (safe, reversible). The root-cause fix.
2. **Verify health** — confirm the service recovered (read-only).
