---
title: Reset checkout feature flag (kafkaQueueProblems)
service: checkout
severity: sev2
tags:
- backpressure
- queue
- kafka
- flag
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/kafkaQueueProblems
  namespace: otel-demo
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/checkout
  namespace: otel-demo
---
# Reset checkout feature flag (kafkaQueueProblems)

Checkout is backing up because the `kafkaQueueProblems` flag is injecting Kafka backpressure.

1. **Reset feature flag** — flip `kafkaQueueProblems` back to `off` (safe, reversible). The root-cause fix.
2. **Verify health** — confirm the service recovered (read-only).
