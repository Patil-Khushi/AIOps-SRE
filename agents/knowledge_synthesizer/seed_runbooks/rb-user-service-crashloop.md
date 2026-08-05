---
id: rb-user-service-crashloop
title: user-service — CrashLoopBackOff from bad database host
service: user-service
version: 1
tags: [crashloop, startup, config, database]
severity: Sev-1
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-08-03
---

## Symptoms
- Pod cycling CrashLoopBackOff with a climbing restartCount.
- `up{namespace="ecommerce"} == 0` for user-service; scrapes fail.
- Container logs end before uvicorn binds — no HTTP access lines at all.

## Affected service & blast radius
`user-service`. Blast radius of the fix: **LOW — correcting one environment variable; rolls forward automatically.**

## Diagnosis
1. `kubectl -n ecommerce describe pod` shows repeated restarts, not OOMKilled.
2. `MYSQL_HOST` on the Deployment points at a host that does not resolve.
3. mysql_client.py reads it via `os.environ[...]` at IMPORT time, so the process dies before serving — this is a startup failure, not a runtime one.

## Resolution steps
1. **[clear_fault · low]** Restore `MYSQL_HOST` to `mysql` on the Deployment.
2. **[healthcheck · none]** Confirm the pod reaches Ready and scrapes resume.

## Verification
- Pod Running and Ready; restartCount stops climbing.
- `up` returns to 1 and `EcommerceServiceDown` clears.

## Rollback
1. Set `MYSQL_HOST` back to a non-resolving value to reproduce.

## Known wrong fixes (do NOT do these)
- Restart the deployment — THE most common wrong move here. The bad value lives in the pod spec, so every new pod inherits it and crashloops identically.
- Scale MySQL up — MySQL is already healthy; the app is pointed at the wrong host.
- Increase the liveness probe's failureThreshold — this hides the crashloop without fixing it, and the service still never serves traffic.

## References
- Scenario: `user_service_crashloop`
- Failure key: `user_service.crashloop`
- Alert rule: `EcommerceServiceDown`
- Truth file: `demo/ecommerce/truth_files/user_service_crashloop.json`
