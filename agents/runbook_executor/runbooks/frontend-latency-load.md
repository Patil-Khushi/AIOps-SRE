---
title: Relieve frontend latency / load (imageSlowLoad, loadGeneratorFloodHomepage)
service: frontend
severity: sev3
tags:
- latency
- load
- traffic
- slow
- saturation
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/imageSlowLoad
  namespace: otel-demo
- name: verify-latency
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/frontend
  namespace: otel-demo
---
# Relieve frontend latency / load

Use when `frontend` p95 latency or request rate spikes — either from the
`imageSlowLoad` flag injecting slow asset loads or a homepage traffic flood.

1. **Reset feature flag** — flip `imageSlowLoad` back to `off` (safe,
   reversible). The root-cause fix for the injected-latency case.
2. **Verify latency** — confirm p95 has recovered (read-only).
   requires approval and scales back down on rollback.
