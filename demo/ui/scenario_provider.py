"""Scenario control plane for the demo UI, backed by the ecommerce SUT.

Replaces the flagd-based control plane the dashboard used when the OpenTelemetry
Demo was the system under test. That version flipped a feature flag in the
`otel-demo` namespace and read current state back from flagd. Both are gone:
flagd shipped with the demo chart, which was removed in migration Phase 6.

The ecommerce equivalent has no flag daemon. A fault is either an env var on a
Deployment or a StatefulSet scaled to zero, applied through
``demo.ecommerce.failure_injection`` (which itself dispatches to kubectl via its
k8s backend). "Is this scenario active?" therefore has to be *read back from the
cluster* rather than looked up in a config map — see ``active_state()``.

The row shape returned by ``load()`` is deliberately unchanged from the flagd
era so the React Overview page needs no modification:

    scenario_id · title · service · severity · alert · description
    category · flag · variant_on · current_variant

``flag`` now carries the failure key (``user_service.mysql_down``) instead of a
flagd flag name. It stays under the old key because the dashboard and several
server helpers index on it; renaming it is a UI change for no behavioural gain.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "ecommerce" / "scenarios"
NAMESPACE = os.environ.get("AIOPS_ECOMMERCE_NAMESPACE", "ecommerce")

# Prometheus alertname per scenario. Must match the `ecommerce` rule group in
# infra/observability/prometheus-values.yaml — the synthetic alert the dashboard
# raises on inject has to carry the same alertname as the real firing rule, or
# the two will not dedup against each other and the operator sees the incident
# twice.
#
# Same mapping as demo/ecommerce/truth_files/_add_eval_blocks.py. Duplicated
# rather than imported: that module is a one-shot generator script, and making
# the UI server import it at startup would couple the request path to a dev tool.
ALERTNAMES: dict[str, str] = {
    "user_service_mysql_down": "EcommerceMySQLDown",
    "order_service_postgres_down": "EcommercePostgresDown",
    "payment_service_redis_down": "EcommerceRedisDown",
    "user_service_crashloop": "EcommerceServiceDown",
    "order_service_memory_leak": "EcommerceServiceDown",
    "order_service_payment_timeout": "EcommercePaymentTimeouts",
    "payment_service_gateway_timeout": "EcommercePaymentTimeouts",
    "order_service_http_500": "EcommerceOrderErrorRateHigh",
    "payment_service_http_500": "EcommerceOrderErrorRateHigh",
    "user_service_high_latency": "EcommerceOrderLatencyHigh",
    "user_service_high_cpu": "EcommerceOrderLatencyHigh",
    "payment_service_high_cpu": "EcommerceOrderLatencyHigh",
}

# Workloads scaled to zero rather than env-toggled. Mirrors _STATEFULSETS in
# demo/ecommerce/failure_injection/_k8s.py.
_DATASTORES = {"mysql", "postgres", "redis"}

# scenario_id -> (workload, env var, value that means "fault active")
# Derived from each failure module's inject(). Used only to READ state back;
# injection itself goes through the failure_injection package so there is one
# implementation of "how to break this".
_ENV_FAULTS: dict[str, tuple[str, str, str]] = {
    "order_service_http_500": ("order-service", "INJECT_HTTP_500", "true"),
    "order_service_memory_leak": ("order-service", "INJECT_MEMORY_LEAK", "true"),
    "order_service_payment_timeout": ("mock-payment-gateway", "INJECT_DELAY_SECONDS", "30"),
    "payment_service_gateway_timeout": ("mock-payment-gateway", "INJECT_DELAY_SECONDS", "30"),
    "payment_service_high_cpu": ("payment-service", "INJECT_CPU_LOAD", "true"),
    "payment_service_http_500": ("payment-service", "INJECT_HTTP_500", "true"),
    "user_service_high_cpu": ("user-service", "INJECT_CPU_LOAD", "true"),
    "user_service_high_latency": ("user-service", "INJECT_LATENCY_SECONDS", "10"),
    "user_service_crashloop": ("user-service", "MYSQL_HOST", "nonexistent-db-host"),
}

# scenario_id -> datastore scaled to zero
_SCALE_FAULTS: dict[str, str] = {
    "user_service_mysql_down": "mysql",
    "order_service_postgres_down": "postgres",
    "payment_service_redis_down": "redis",
}


def _failure_key(scenario_id: str) -> str:
    """``user_service_mysql_down`` -> ``user_service.mysql_down``.

    The scenario YAML carries ``failure_key`` explicitly; this is the fallback
    for a file that omits it. Only the first underscore-pair is a service
    prefix — ``order_service_memory_leak`` must become
    ``order_service.memory_leak``, not ``order.service_memory_leak``.
    """
    for prefix in ("user_service", "order_service", "payment_service"):
        if scenario_id.startswith(prefix + "_"):
            return f"{prefix}.{scenario_id[len(prefix) + 1 :]}"
    return scenario_id


def load() -> dict[str, dict[str, Any]]:
    """Read every scenario YAML into the row shape the dashboard renders."""
    out: dict[str, dict[str, Any]] = {}
    if not SCENARIOS_DIR.exists():
        logger.warning("scenario dir %s does not exist; catalog will be empty", SCENARIOS_DIR)
        return out

    for path in sorted(SCENARIOS_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError(f"scenario file {path.name} must be a YAML mapping")
        sid = data.get("id") or path.stem
        if sid != path.stem:
            raise RuntimeError(
                f"scenario file {path.name}: 'id' must equal filename stem "
                f"{path.stem!r}, got {sid!r}"
            )
        detection = data.get("detection") or {}
        out[sid] = {
            "title": data.get("title") or sid,
            "service": data.get("service") or "unknown",
            "severity": data.get("severity") or "high",
            "alert": ALERTNAMES.get(sid, "EcommerceServiceDown"),
            "description": detection.get("l1") or data.get("expected_rca") or "",
            # Grouped by service on the Overview page.
            "category": data.get("service") or "ecommerce",
            "flag": data.get("failure_key") or _failure_key(sid),
            "variant_on": "on",
            "expected_rca": data.get("expected_rca") or "",
            "settle_seconds": data.get("settle_seconds") or 20,
        }
    return out


def _kubectl(args: list[str], timeout: int = 15) -> str | None:
    """Run kubectl and return stdout, or None on any failure.

    Never raises: this feeds a status column. A cluster that is down should grey
    the column out, not 500 the whole scenarios page.
    """
    try:
        r = subprocess.run(
            ["kubectl", "-n", NAMESPACE, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("kubectl %s failed: %s", " ".join(args), exc)
        return None
    if r.returncode != 0:
        logger.debug("kubectl %s exit=%d: %s", " ".join(args), r.returncode, r.stderr.strip())
        return None
    return r.stdout


def active_state(scenarios: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Return ``{scenario_id: "on"|"off"}`` read back from the live cluster.

    Two kubectl calls total, not one per scenario — the Overview page polls
    this, and twelve exec()s per poll would be visibly slow on Windows.
    """
    state = dict.fromkeys(scenarios, "off")

    # Env-toggled faults: one pass over all deployments.
    envs: dict[str, dict[str, str]] = {}
    out = _kubectl(
        [
            "get",
            "deployments",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{'\\t'}"
            "{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{','}{end}{'\\n'}{end}",
        ]
    )
    if out:
        for line in out.splitlines():
            if "\t" not in line:
                continue
            name, _, envstr = line.partition("\t")
            pairs = {}
            for kv in envstr.split(","):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    pairs[k.strip()] = v.strip()
            envs[name.strip()] = pairs

    for sid, (workload, key, want) in _ENV_FAULTS.items():
        if sid in state and envs.get(workload, {}).get(key) == want:
            state[sid] = "on"

    # Scaled-to-zero faults: one pass over all statefulsets.
    out = _kubectl(
        [
            "get",
            "statefulsets",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{'\\t'}{.spec.replicas}{'\\n'}{end}",
        ]
    )
    if out:
        replicas = {}
        for line in out.splitlines():
            if "\t" in line:
                n, _, r = line.partition("\t")
                replicas[n.strip()] = r.strip()
        for sid, ds in _SCALE_FAULTS.items():
            if sid in state and replicas.get(ds) == "0":
                state[sid] = "on"

    return state


def _run_failure(failure_key: str, action: str) -> dict[str, Any]:
    """Call inject()/recover() on the failure_injection package via orchestrator."""
    # Imported lazily so a missing/py-broken SUT package degrades this one
    # endpoint instead of preventing the whole UI server from starting.
    from demo.ecommerce.failure_injection import FAILURES, inject, recover

    failure = FAILURES.get(failure_key)
    if failure is None:
        return {"ok": False, "error": f"unknown failure key {failure_key!r}"}
    try:
        result = inject(failure) if action == "inject" else recover(failure)
        return {
            "ok": result["ok"],
            "failure_key": failure_key,
            "action": action,
            "orchestrator_result": result,
        }
    except Exception as exc:
        logger.exception("%s %s failed", action, failure_key)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def inject(scenario_id: str, scenarios: dict[str, dict[str, Any]]) -> dict[str, Any]:
    s = scenarios.get(scenario_id)
    if not s:
        return {"ok": False, "error": f"unknown scenario {scenario_id!r}"}
    return _run_failure(str(s["flag"]), "inject")


def reset(scenario_id: str, scenarios: dict[str, dict[str, Any]]) -> dict[str, Any]:
    s = scenarios.get(scenario_id)
    if not s:
        return {"ok": False, "error": f"unknown scenario {scenario_id!r}"}
    return _run_failure(str(s["flag"]), "recover")


def reset_all(scenarios: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Recover every scenario.

    Not atomic, unlike the flagd version — that wrote one ConfigMap patch for
    all flags. Here each recovery is its own kubectl call, so a partial failure
    is possible; the per-scenario results say which ones landed.
    """
    results = []
    for sid in scenarios:
        r = reset(sid, scenarios)
        results.append({"scenario_id": sid, **r})
    touched = [r["scenario_id"] for r in results if r.get("ok")]
    return {"reset_count": len(touched), "touched": touched, "results": results}
