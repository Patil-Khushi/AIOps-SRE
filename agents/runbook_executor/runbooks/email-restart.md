---
title: Restart email deployment
service: email
severity: null
tags: [restart, pods, availability]
steps:
  - name: drain-connections
    action: drain
    destructive: false
    idempotent: true
    target: deployment/email
    namespace: otel-demo
  - name: restart-pods
    action: restart_deployment
    destructive: true
    idempotent: true
    rollback_action: rescale_previous
    target: deployment/email
    namespace: otel-demo
---

# Restart email deployment

Roll the `email` deployment to clear bad pod state (crash loop, wedged connections). The restart is destructive and is held at the HITL gate; it rolls back to the previous scale if it does not come up healthy.
