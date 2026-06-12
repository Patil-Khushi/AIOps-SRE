---
id: rb-recommendation-cache-failure
title: Recommendation service — cache bypass, elevated p95 latency
service: recommendation
version: 1
tags: [latency, cache, flagd, feature-flag, recommendation]
severity: Sev-2
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-06-11
---

## Symptoms
- `RecommendationLatencyP95High` alert firing.
- `histogram_quantile(0.95, ...service_name="recommendation")` rising; duration up across the board.
- Latency climbs above target while error rate stays normal.

## Affected service & blast radius
`recommendation`. Blast radius of the fix: **LOW** — single feature flag, instant rollback.

## Diagnosis
1. Confirm p95 latency is elevated but errors are not — points to a slow path, not an outage.
2. Read flagd `flagd-config`: `recommendationCacheFailure == on` makes the service skip its in-memory cache and recompute every request.
3. Rule out cache backend (Valkey/Redis) unreachable and recent deploy that removed the caching path.

## Resolution steps
1. **[set_flag · low]** Flip `recommendationCacheFailure` to `off` in the flagd ConfigMap (via the feature-flags seam).
2. **[manual · medium]** Once the flag is off, warm the cache by issuing a handful of GetRecommendations requests.

## Verification
- p95 latency returns to target within a minute or two of the flag flip + cache warm.
- `RecommendationLatencyP95High` clears.

## Rollback
1. Flip `recommendationCacheFailure` back to its previous variant in the flagd ConfigMap.
2. `kubectl rollout undo` for the recommendation deployment.

## Known wrong fixes (do NOT do these)
- Increase recommendation pod CPU limits — the latency is from cache miss, not CPU saturation.

## References
- Scenario: `recommendation_cache_failure`
- flagd flag: `recommendationCacheFailure`
