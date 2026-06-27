---
title: Clear product-catalog latency injection (productCatalogFailure)
service: product-catalog
severity: sev1
tags:
- latency
- slow
- p95
- flag
- high-latency
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/productCatalogFailure
  namespace: otel-demo
- name: verify-latency
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/product-catalog
  namespace: otel-demo
---
# Clear product-catalog latency injection

Use when `product-catalog` p95 latency climbs toward ~5s because the
`productCatalogFailure` feature flag is injecting a deterministic delay.

1. **Reset feature flag** — flip `productCatalogFailure` back to `off` (safe,
   reversible). This is the actual root-cause fix.
2. **Verify latency** — confirm p95 has recovered (read-only).
   Destructive: requires approval and rolls back to the previous scale if the
   restart does not come up healthy.
