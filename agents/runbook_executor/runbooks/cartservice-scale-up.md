---
title: Scale up cartservice under sustained load
service: cart
severity: sev2
tags: [latency, load, saturation, high-latency]
steps:
  - name: snapshot-replicas
    action: snapshot_replicas
    destructive: false
    idempotent: true
    target: deployment/cart
    namespace: otel-demo
  - name: scale-up
    action: scale_deployment
    destructive: true
    idempotent: true
    rollback_action: scale_down
    target: deployment/cart
    namespace: otel-demo
  - name: verify-health
    action: healthcheck
    destructive: false
    idempotent: true
    target: deployment/cart
    namespace: otel-demo
---

# Scale up cartservice

Use when `cart` p95 latency climbs under sustained load.

1. **Snapshot replicas** — record current replica count for rollback (safe).
2. **Scale up** — increase replicas. Destructive: requires approval; rolls back
   to the snapshotted count on failure.
3. **Verify health** — confirm the new replicas pass readiness (safe).
