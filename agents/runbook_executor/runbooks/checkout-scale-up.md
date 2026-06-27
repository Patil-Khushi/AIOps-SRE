---
title: Scale up checkout under load
service: checkout
severity: null
tags: [load, saturation, scale, capacity]
steps:
  - name: snapshot-replicas
    action: snapshot_replicas
    destructive: false
    idempotent: true
    target: deployment/checkout
    namespace: otel-demo
  - name: scale-up
    action: scale_deployment
    destructive: true
    idempotent: true
    rollback_action: scale_down
    target: deployment/checkout
    namespace: otel-demo
  - name: verify-health
    action: healthcheck
    destructive: false
    idempotent: true
    target: deployment/checkout
    namespace: otel-demo
---

# Scale up checkout under load

Add replicas to `checkout` to absorb sustained load / saturation. The scale step is destructive (held at the HITL gate) and scales back down on rollback.
