---
title: user-service — CrashLoopBackOff on startup
service: user-service
severity: sev1
tags:
- crashloop
- startup
- config
- database
steps:
- name: clear-injected-fault
  action: clear_fault
  destructive: true
  idempotent: true
  target: fault/user_service.crashloop
  namespace: ecommerce
- name: verify-health
  action: healthcheck
  destructive: false
  idempotent: true
  target: deployment/user-service
  namespace: ecommerce
---
# user-service — CrashLoopBackOff on startup

| | |
|---|---|
| **Alert** | `EcommerceServiceDown` |
| **Service** | `user-service` |
| **Severity** | `sev1` |

## 1. Symptoms

Pod cycling through CrashLoopBackOff; `up{namespace="ecommerce"} == 0` for user-service; restartCount climbing.

## 2. Confirm it is this failure

Several ecommerce alerts are raised by more than one scenario, so the
alert name alone does not identify the fault. Run these first.

**Is the pod actually crashlooping?**

```powershell
kubectl -n ecommerce get pods -l app=user-service
```

Expect: CrashLoopBackOff with a climbing RESTARTS count.

**Why did the container exit?**

```powershell
kubectl -n ecommerce describe pod -l app=user-service | Select-String -Pattern 'Reason|Exit Code' 
```

Expect: Reason: Error (NOT OOMKilled - that is the memory-leak runbook).

**What is MYSQL_HOST set to?**

```powershell
kubectl -n ecommerce get deploy user-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="MYSQL_HOST")].value}'
```

Expect: A host that does not resolve, e.g. nonexistent-db-host. Healthy value is `mysql`.

## 3. Root cause

`MYSQL_HOST` points at a host that does not resolve. mysql_client.py reads it with `os.environ[...]` at import time, so the process raises before uvicorn binds — the container exits and Kubernetes backs off.

## 4. Procedure

### Step 1. clear-injected-fault

`clear_fault` &middot; **destructive - needs approval**

Undo the injected fault. This is the root-cause fix.

Manual equivalent (Kubernetes):

```powershell
uv run --no-project python -m failure_injection recover user_service.crashloop
```

Manual equivalent (Docker Compose):

```powershell
FI_BACKEND=docker uv run --no-project python -m failure_injection recover user_service.crashloop
```

Expect: The workload rolls; the fault toggle returns to its default.

### Step 2. verify-health

`healthcheck` &middot; read-only

Read-only check that the service recovered.

Manual equivalent (Kubernetes):

```powershell
kubectl -n ecommerce rollout status deploy/user-service --timeout=120s
curl http://localhost:30081/health
```

Manual equivalent (Docker Compose):

```powershell
curl http://localhost:8001/health
```

Expect: HTTP 200 with {"status":"ok"} and every dependency true.

## 5. Verify the fix

**Pod is Ready and stable**

```
kubectl -n ecommerce get pods -l app=user-service
```

Expect: 1/1 Running; RESTARTS stops climbing.

**Prometheus can scrape it again**

```
up{namespace="ecommerce",service_name="ecommerce-user-service"}
```

Expect: 1

## 6. If that did not fix it

- A restart alone will NOT help: the bad value lives in the pod spec, so every new pod inherits it. The env var must be corrected first.
- If MYSQL_HOST is already `mysql` and it still crashloops, the container is failing for another reason - read the logs of the PREVIOUS attempt: `kubectl -n ecommerce logs deploy/user-service --previous`.
- Re-apply the manifest to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.
- Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.
- List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.
- Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.

## Notes

- A restart alone will NOT fix this: the bad value lives in the pod spec, so every new pod inherits it. The env var has to be corrected first.
- Distinguish from mysql_down: there MySQL is missing but the host resolves, so the service starts and serves 500s instead of crashlooping.
