---
title: Roll back a bad checkout deployment
service: checkout
severity: sev1
tags: [deploy, config, bad-deploy, regression]
steps:
  - name: rollback-deploy
    action: rollback_deployment
    destructive: true
    idempotent: false
    rollback_action: redeploy_current
    target: deployment/checkout
    namespace: otel-demo
  - name: clear-cache
    action: flush_cache
    destructive: false
    idempotent: true
    target: deployment/checkout
    namespace: otel-demo
---

# Roll back checkout

Use when a recent `checkout` deploy introduced a regression.

1. **Roll back deploy** — revert to the previous known-good revision.
   Destructive: requires approval; re-applies the current revision on failure.
2. **Clear cache** — flush stale cached config so the rollback takes effect (safe).
