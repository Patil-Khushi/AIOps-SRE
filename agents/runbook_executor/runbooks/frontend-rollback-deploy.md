---
title: Roll back the last frontend deployment
service: frontend
severity: null
tags: [deploy, rollback, regression, bad-deploy]
steps:
  - name: snapshot-current
    action: snapshot_replicas
    destructive: false
    idempotent: true
    target: deployment/frontend
    namespace: otel-demo
  - name: rollback-deploy
    action: rollback_deployment
    destructive: true
    idempotent: true
    rollback_action: redeploy_current
    target: deployment/frontend
    namespace: otel-demo
---

# Roll back the last frontend deployment

Revert `frontend` to the previous known-good deployment when a recent rollout introduced the regression. Destructive: held at the HITL gate, and re-deploys the current revision on rollback.
