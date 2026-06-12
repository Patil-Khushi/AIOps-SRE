---
id: rb-product-catalog-latency
title: Product Catalog service — high GetProduct latency
service: productcatalogservice
version: 1
tags: [latency, flagd, feature-flag, product-catalog]
severity: Sev-2
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-06-11
---

## Symptoms
- `ProductCatalogLatencyP95High` alert firing; latency_p95 crosses ~1s and climbs toward 5s.
- `histogram_quantile(0.95, rate(http_server_duration_bucket{service_name="productcatalogservice"}[1m]))` crosses 5s.
- Trace spans `service.name=productcatalogservice operation=GetProduct` show duration > 4500ms.
- Slowness looks like an upstream/thread-pool stall from the outside, but is **inside** the service.

## Affected service & blast radius
`productcatalogservice`. Blast radius of the fix: **LOW** — a single feature flag, instant rollback.

## Diagnosis
1. Inspect GetProduct trace spans — confirm the delay is intra-service, not in a downstream dependency.
2. Read the flagd `flagd-config` ConfigMap: `productCatalogFailure.defaultVariant == "on"` injects a deterministic ~5s delay.
3. Correlate flag-flip timestamp with latency onset.

## Resolution steps
1. **[set_flag · low]** Set flagd flag `productCatalogFailure` defaultVariant back to `off` via the feature-flags seam.
2. **[rollback_deploy · medium]** If the flag did not actually change recently, roll back the most recent productcatalogservice deploy.

## Verification
- GetProduct p95 returns below the 1s threshold (toward ~200ms baseline) within ~1 min of the flag flip.
- `ProductCatalogLatencyP95High` clears.

## Rollback
1. Re-flip `productCatalogFailure` back to `on` — instant.
2. `helm rollback otel-demo` to the prior revision.

## Known wrong fixes (do NOT do these)
- Restart the productcatalogservice pod — a restart does not unset a feature flag; the issue returns immediately.
- Scale productcatalogservice horizontally — every replica injects the same delay; scaling does nothing.
- Increase frontend HTTP timeouts — masks the symptom, not the cause.

## References
- Scenario: `slow-product-catalog`
- flagd flag: `productCatalogFailure`
