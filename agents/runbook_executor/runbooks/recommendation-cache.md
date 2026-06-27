---
title: Restore recommendation cache (recommendationCacheFailure)
service: recommendation
severity: sev2
tags:
- latency
- cache
- flag
- slow
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/recommendationCacheFailure
  namespace: otel-demo
- name: flush-cache
  action: flush_cache
  destructive: false
  idempotent: true
  target: deployment/recommendation
  namespace: otel-demo
---
# Restore recommendation cache

Use when `recommendation` latency climbs because the
`recommendationCacheFailure` flag makes the service skip its in-memory cache and
recompute every request.

1. **Reset feature flag** — flip `recommendationCacheFailure` back to `off`
   (safe, reversible). This is the root-cause fix.
2. **Flush cache** — warm/clear the in-memory cache so it repopulates cleanly
   (read-only / non-destructive).
   approval and rolls back to the previous scale if it does not come up healthy.
