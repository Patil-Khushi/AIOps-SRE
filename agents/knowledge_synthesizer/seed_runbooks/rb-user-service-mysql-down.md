---
id: rb-user-service-mysql-down
title: user-service — MySQL unavailable, all logins failing
service: user-service
version: 1
tags: [database, mysql, dependency, login, 5xx]
severity: Sev-1
source: seed
source_incident: null
status: published
related_kb: null
last_updated: 2026-08-03
---

## Symptoms
- `EcommerceMySQLDown` firing; `mysql_connection_status == 0`.
- POST /login and /register return HTTP 500.
- Logs: `database connection failed`, connection refused to `mysql:3306`.
- Order creation also fails with 401 — order-service validates every order against user-service /profile.

## Affected service & blast radius
`user-service`. Blast radius of the fix: **LOW — scaling one StatefulSet back up; the PVC is retained so no data is lost.**

## Diagnosis
1. Check `kubectl -n ecommerce get statefulset mysql` — replicas at 0 means the datastore was scaled down, not crashed.
2. user-service pods are Running, not CrashLoopBackOff. That distinguishes this from the crashloop scenario, where MYSQL_HOST itself is wrong.
3. /health returns HTTP 200 with `status: degraded` — deliberately, so a database outage does not restart the app pod.

## Resolution steps
1. **[clear_fault · low]** Scale the MySQL StatefulSet back to 1 and wait for the rollout.
2. **[healthcheck · none]** Confirm `mysql_connection_status == 1` and a login succeeds.

## Verification
- `mysql_connection_status` returns to 1 within ~30s.
- `EcommerceMySQLDown` clears.
- A register → login → order round trip succeeds.

## Rollback
1. Scale the MySQL StatefulSet back to 0 to reproduce the fault.

## Known wrong fixes (do NOT do these)
- Restart user-service — the app is healthy; its dependency is missing. A restart changes nothing and adds downtime.
- Scale up user-service replicas — every replica fails identically. Capacity is not the constraint.
- Delete the MySQL PVC to 'start clean' — this destroys all user accounts and does not address the scale-to-zero.

## References
- Scenario: `user_service_mysql_down`
- Failure key: `user_service.mysql_down`
- Alert rule: `EcommerceMySQLDown`
- Truth file: `demo/ecommerce/truth_files/user_service_mysql_down.json`
