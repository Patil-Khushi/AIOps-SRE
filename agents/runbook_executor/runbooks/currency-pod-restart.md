---
title: Recover currency service after a pod kill (currency-pod-kill)
service: currency
severity: sev2
tags: [crash, restart, pod, crashloop, availability]
steps:
  - name: verify-pods
    action: healthcheck
    destructive: false
    idempotent: true
    target: deployment/currency
    namespace: otel-demo
  - name: restart-deployment
    action: restart_deployment
    destructive: true
    idempotent: true
    rollback_action: rescale_previous
    target: deployment/currency
    namespace: otel-demo
---

# Recover currency service after a pod kill

Use when a `currency` pod was killed (chaos / node pressure) and the service is
degraded or in CrashLoopBackOff.

1. **Verify pods** — check how many replicas are healthy (read-only).
2. **Restart deployment** — roll the deployment so killed pods are recreated
   cleanly. Destructive: requires approval and rolls back to the previous scale
   if it does not come up healthy.
