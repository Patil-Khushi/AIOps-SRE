"""Kubernetes backend for failure injection.

Implements the same four verbs as ``_docker`` — stop / start / apply_override /
remove_override — against the ``ecommerce`` namespace in Rancher Desktop's k3s.

Why k8s is the better target than Compose for these scenarios:

  * ``memory_leak_oom`` produces a real ``OOMKilled`` terminated-reason and an
    incrementing ``restartCount``, not just a dead container.
  * ``crashloop`` produces genuine ``CrashLoopBackOff`` with exponential
    backoff, which is the state the truth file actually claims.
  * The remediation agents speak kubectl, so their fixes are real rather
    than simulated.

Set FI_DRY_RUN=1 to print commands instead of running them.
"""

from __future__ import annotations

import json
import os
import subprocess

from . import _kubectl

NAMESPACE = os.getenv("FI_NAMESPACE", "ecommerce")
DRY_RUN = os.getenv("FI_DRY_RUN", "").lower() in ("1", "true", "yes")

# Datastores are StatefulSets (stable identity + retained PVC); the app tier is
# Deployments. `kubectl scale` needs the kind, so the mapping is explicit —
# guessing from the name would break the moment something is renamed.
_STATEFULSETS = {"mysql", "postgres", "redis"}

# Fault toggles per workload, with the healthy-baseline value for each.
# remove_override() writes these values back rather than deleting the keys.
#
# Why restore-to-default instead of `kubectl set env KEY-`: deleting removes the
# entry from the pod spec entirely, including the `valueFrom: configMapKeyRef`
# mapping the manifest declares (e.g. ORDER_INJECT_HTTP_500 -> INJECT_HTTP_500).
# Behaviour stays correct, because faults.py reads os.getenv(KEY, "<default>") —
# but the workload silently stops honouring the ConfigMap, so the
# patch-the-ConfigMap injection path documented in k8s/README.md breaks after
# the first inject/recover cycle. Writing explicit defaults keeps the key set
# stable and makes recovery idempotent.
#
# These MUST match the defaults in demo/ecommerce/k8s/01-config.yaml.
_FAULT_DEFAULTS: dict[str, dict[str, str]] = {
    "user-service": {
        "INJECT_LATENCY_SECONDS": "0",
        "INJECT_CPU_LOAD": "false",
        "MYSQL_HOST": "mysql",
    },
    "order-service": {
        "INJECT_HTTP_500": "false",
        "INJECT_MEMORY_LEAK": "false",
    },
    "payment-service": {
        "INJECT_HTTP_500": "false",
        "INJECT_CPU_LOAD": "false",
    },
    "mock-payment-gateway": {
        "INJECT_DELAY_SECONDS": "0",
    },
}


def _workload(service: str) -> str:
    kind = "statefulset" if service in _STATEFULSETS else "deployment"
    return f"{kind}/{service}"


def run(args: list[str]) -> int:
    """Run kubectl with the namespace pre-applied."""
    cmd = [_kubectl.resolve(), "-n", NAMESPACE, *args]
    printable = " ".join(["kubectl", "-n", NAMESPACE, *args])
    if DRY_RUN:
        print(f"[dry-run] {printable}")
        return 0
    print(f"+ {printable}")
    return subprocess.call(cmd)


# --- scale to zero / back ---------------------------------------------------
def stop(service: str) -> None:
    """Scale a workload to 0 — the '<datastore> down' failures."""
    run(["scale", _workload(service), "--replicas=0"])


def start(service: str) -> None:
    run(["scale", _workload(service), "--replicas=1"])
    # Block until it is actually serving again, so a recover() immediately
    # followed by a verification query doesn't race the rollout.
    if service in _STATEFULSETS:
        run(["rollout", "status", _workload(service), "--timeout=180s"])


# --- env / resource overrides -----------------------------------------------
def apply_override(service: str, config: dict) -> None:
    """Apply a fault to `service`.

    Accepts the same config dicts the Compose backend takes:
      * ``environment``: {KEY: VALUE}  -> kubectl set env (triggers a rollout)
      * ``mem_limit``:   "256m"        -> patched onto the container's limits
      * ``restart``:     ignored       -> Deployments always restart; this key
                                          exists only for Compose parity
    """
    env = config.get("environment") or {}
    if env:
        pairs = [f"{k}={v}" for k, v in env.items()]
        # `set env` edits the pod template, which rolls the workload
        # automatically — no separate restart needed.
        run(["set", "env", _workload(service), *pairs])

    if mem := config.get("mem_limit"):
        # Compose uses "256m"; Kubernetes wants "256Mi".
        qty = mem.replace("m", "Mi") if mem.endswith("m") else mem
        patch = json.dumps(
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {"name": service, "resources": {"limits": {"memory": qty}}}
                            ]
                        }
                    }
                }
            }
        )
        run(["patch", _workload(service), "--type", "strategic", "-p", patch])


def remove_override(service: str) -> None:
    """Restore every fault toggle for `service` to its healthy default."""
    defaults = _FAULT_DEFAULTS.get(service, {})
    if defaults:
        pairs = [f"{k}={v}" for k, v in defaults.items()]
        run(["set", "env", _workload(service), *pairs])

    # order-service is the only workload whose manifest pins a non-default
    # limit (256Mi, chosen so the leak OOMKills promptly). Re-assert it so a
    # mem_limit override cannot leak into later scenarios.
    if service == "order-service":
        patch = json.dumps(
            {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "order-service",
                                    "resources": {"limits": {"memory": "256Mi"}},
                                }
                            ]
                        }
                    }
                }
            }
        )
        run(["patch", _workload(service), "--type", "strategic", "-p", patch])
