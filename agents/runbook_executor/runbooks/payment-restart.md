---
title: Restart payment-service after an OOM crash loop
service: payment
severity: sev1
tags: [crash, oom, restart, crashloop]
steps:
  - name: drain-connections
    action: drain
    destructive: false
    idempotent: true
    target: deployment/payment
    namespace: otel-demo
  - name: restart-pods
    action: restart_deployment
    destructive: true
    idempotent: true
    rollback_action: rescale_previous
    target: deployment/payment
    namespace: otel-demo
---

# Restart payment-service

Use when `payment` is in a CrashLoopBackOff after an out-of-memory kill.

1. **Drain connections** — stop new traffic to the pods (safe, reversible).
2. **Restart pods** — roll the deployment. Destructive: requires approval, and
   rolls back to the previously-scaled state if the restart does not come up
   healthy.
