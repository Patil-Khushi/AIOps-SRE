---
title: Recover email service from a memory leak (emailMemoryLeak)
service: email
severity: sev3
tags:
- memory
- oom
- leak
- crash
- restart
steps:
- name: reset-feature-flag
  action: reset_feature_flag
  destructive: true
  idempotent: true
  target: flag/emailMemoryLeak
  namespace: otel-demo
- name: capture-heap-snapshot
  action: snapshot_replicas
  destructive: false
  idempotent: true
  target: deployment/email
  namespace: otel-demo
---
# Recover email service from a memory leak

Use when `email` RSS climbs past its threshold because the `emailMemoryLeak`
feature flag is leaking memory on every send.

1. **Reset feature flag** — flip `emailMemoryLeak` back to `off` (safe,
   reversible). This is the root-cause fix.
2. **Capture heap snapshot** — grab a snapshot for later analysis (read-only).
   approval and rolls back to the previous scale if it does not come up healthy.
