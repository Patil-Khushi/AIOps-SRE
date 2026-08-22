"""Generate the runbook library from the ecommerce failure scenarios.

Replaces the 30 hand-written runbooks that targeted OTel Demo services (ad,
cart, checkout, currency, email, frontend, payment, product-catalog,
recommendation). Every one of those services was deleted with the demo app, so
every runbook pointed at a workload that no longer exists — the selector would
happily match one and the executor would then fail against a missing Deployment.

Generated rather than hand-written so the runbook, the scenario YAML, the truth
file and the Prometheus alert cannot drift apart: the mapping lives in one table
below. This mirrors the existing scripts/generate_truth_files.py convention.

    uv run python scripts/generate_runbooks.py

Idempotent — output is a pure function of the table.

Step actions must come from the vocabulary the executor understands:
    clear_fault · healthcheck · restart_deployment · scale_deployment
    snapshot_replicas · rollback_deployment · redeploy_current · drain
    flush_cache · rescale_previous · scale_down

`clear_fault` with `target: fault/<failure_key>` is the one that performs a REAL
recovery, via the automation.fault.clear seam (demo/ui/fault_clear.py). Anything
else is simulated by the mock provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "agents" / "runbook_executor" / "runbooks"
NAMESPACE = "ecommerce"


@dataclass
class Step:
    name: str
    action: str
    target: str
    destructive: bool = False
    idempotent: bool = True
    # Emitted only when set. The executor treats a destructive step with a
    # rollback_action as reversible, which is what lets the HITL gate offer
    # "approve, and undo automatically if it does not come up healthy".
    rollback_action: str | None = None
    # ── body-only fields (never rendered into frontmatter) ──────────────────
    # what the step actually does, in plain language
    what: str = ""
    # the manual equivalent, so a human can run it without the executor
    manual_k8s: str = ""
    manual_compose: str = ""
    # what you should see afterwards
    expect: str = ""


SEED_OWNER = "sre-platform"
# The seed library's approval is the repo's own PR review — every one of these files
# is generated from this table and merged through CI. Naming that here, rather than a
# person, keeps `approved_by` truthful: no individual signed a per-runbook approval.
SEED_APPROVER = "aiops-sre-review"
SEED_ENVIRONMENTS = ["demo", "production"]

# fault key -> (Prometheus alertname, generic failure category, observed signals).
#
# ONE table, because these three facets are what the executor matches an incident
# against, and they were previously spread across three files that drifted: the
# alertname also lives in demo/ui/scenario_provider.py::ALERTNAMES (which corrected
# three runbooks that named an alert their fault does not fire — high-CPU pointing at
# an order-service latency rule, the memory leak pointing at a rule that only fires
# after the OOMKill). tests/test_runbook_applicability.py asserts this table still
# agrees with that mapping and with the real rules in
# infra/observability/prometheus-values.yaml, so the drift cannot come back.
#
# The categories are deliberately generic SRE shapes rather than fault names: an
# incident arrives carrying "a dependency is unavailable", not "mysql_down".
FAULT_FACETS: dict[str, tuple[str, str, list[str]]] = {
    "user_service.mysql_down": (
        "EcommerceMySQLDown",
        "dependency_unavailable",
        ["dependency_unavailable", "error_rate_high"],
    ),
    "order_service.postgres_down": (
        "EcommercePostgresDown",
        "dependency_unavailable",
        ["dependency_unavailable", "error_rate_high"],
    ),
    "payment_service.redis_down": (
        "EcommerceRedisDown",
        "dependency_unavailable",
        ["dependency_unavailable", "error_rate_high"],
    ),
    "user_service.crashloop": (
        "EcommerceServiceDown",
        "pod_crashloop",
        ["service_down", "pod_restarting"],
    ),
    "order_service.payment_timeout": (
        "EcommercePaymentTimeouts",
        "dependency_timeout",
        ["timeouts", "latency_high"],
    ),
    "payment_service.gateway_timeout": (
        "EcommercePaymentTimeouts",
        "dependency_timeout",
        ["timeouts", "latency_high"],
    ),
    "order_service.http_500": (
        "EcommerceOrderErrorRateHigh",
        "application_error",
        ["error_rate_high"],
    ),
    "payment_service.http_500": (
        "EcommerceOrderErrorRateHigh",
        "application_error",
        ["error_rate_high"],
    ),
    "user_service.high_latency": (
        "EcommerceOrderLatencyHigh",
        "latency_degradation",
        ["latency_high"],
    ),
    "user_service.high_cpu": (
        "EcommerceUserServiceCPUHigh",
        "resource_saturation_cpu",
        ["cpu_saturation", "latency_high"],
    ),
    "payment_service.high_cpu": (
        "EcommercePaymentServiceCPUHigh",
        "resource_saturation_cpu",
        ["cpu_saturation", "latency_high"],
    ),
    "order_service.memory_leak_oom": (
        "EcommerceOrderServiceMemoryHigh",
        "resource_saturation_memory",
        ["memory_saturation", "pod_restarting"],
    ),
    # ── the five infrastructure-layer failures ──────────────────────────────
    #
    # These have no scenario YAML (demo/ecommerce/scenarios/ holds the twelve
    # application/hybrid ones only), so they never reach the executor through the
    # synthetic-alert path — they arrive from a REAL Prometheus rule, or from a
    # manually raised incident. The category and signals below are therefore taken
    # from demo/ui/runbook_routes.py::ALERT_CATEGORY / ALERT_SIGNALS, which is what
    # actually translates a live alert into matching facets. Inventing a prettier
    # category here would produce a runbook that never matches its own fault.
    "user_service.pool_exhaustion": (
        "EcommerceUserLoginFailures",
        "application_error",
        ["error_rate_high"],
    ),
    # No pod_restarting: unlike memory_leak_oom, external pressure does NOT
    # OOMKill the container (the kernel reclaims, and the hog outranks the app on
    # oom_score) — measured 81 -> 255 MiB with restartCount unchanged. Requiring a
    # restart signal would make the runbook miss its own fault.
    "order_service.memory_exhaust": (
        "EcommerceOrderServiceMemoryHigh",
        "resource_saturation_memory",
        ["memory_saturation"],
    ),
    "payment_service.dns_failure": (
        "EcommercePaymentGatewayUnreachable",
        "dependency_unavailable",
        ["dependency_unavailable"],
    ),
    # An empty alertname means "no Prometheus rule exists for this fault", and
    # facets() then declares no alert constraint at all rather than a fake one.
    # Both of these are deliberate holes, documented in
    # infra/observability/prometheus-values.yaml and docs/failure_reference.md:
    # a 256MB write to the node's ~1TB shared overlay is 0.025% and undetectable,
    # and packet loss needs tc/CAP_NET_ADMIN that this cluster's images lack.
    # The category still constrains them, so a disk runbook stays NOT_APPLICABLE
    # to a memory incident instead of matching everything on its service.
    "payment_service.disk_full": (
        "",
        "resource_saturation_disk",
        ["disk_saturation"],
    ),
    "order_service.packet_loss": (
        "",
        "network_degradation",
        ["packet_loss"],
    ),
}


@dataclass
class RB:
    slug: str
    title: str
    service: str
    severity: str
    tags: list[str]
    alert: str
    symptom: str
    cause: str
    steps: list[Step]
    notes: list[str] = field(default_factory=list)
    # (description, command, expected output) — run these BEFORE acting, to
    # confirm this runbook is the right one. Several ecommerce alerts are
    # raised by more than one scenario, so "which alert fired" is not enough
    # on its own to identify the fault.
    diagnose: list[tuple[str, str, str]] = field(default_factory=list)
    # (description, command-or-PromQL, expected result) — run these AFTER.
    verify: list[tuple[str, str, str]] = field(default_factory=list)
    # what to try when the procedure above did not resolve it
    if_stuck: list[str] = field(default_factory=list)
    # Extra alertnames this runbook should also be a candidate under, beyond the one
    # its fault key declares. Exactly one fault needs this: breaking DNS also raises
    # EcommerceRedisDown (payment-service re-pings Redis inside /metrics and the gauge
    # zeroes on ANY exception, name resolution included). Without this, an incident
    # raised off the Redis alert CONTRADICTS the DNS runbook's alert facet and filters
    # it out — leaving the redis-down runbook top-ranked, which would remediate a
    # perfectly healthy datastore. Being offered as a co-candidate under a related
    # alert costs a glance; being absent costs the wrong fix.
    also_alerts: list[str] = field(default_factory=list)
    # A catch-all recovery for "this service is degraded and we don't know why".
    # Generic runbooks declare NO alert / category / signal constraints, so they stay
    # applicable to any incident on their service and rank below a specific runbook
    # instead of being filtered out. (§4's RB-ORDER-RESTART slot.)
    generic: bool = False


# Host ports differ per deployment. k8s exposes NodePorts; Compose publishes
# 8001-8004. Runbooks show both because both are runnable (FI_BACKEND).
PORTS = {
    "user-service": (30081, 8001),
    "order-service": (30082, 8002),
    "payment-service": (30083, 8003),
}


def _clear(key: str, *, what: str = "", k8s: str = "", compose: str = "", expect: str = "") -> Step:
    # destructive=True because it mutates a running workload and triggers a
    # rollout. It is fully reversible (re-inject restores the fault), but the
    # HITL gate should still see it as an action, not a read.
    return Step(
        "clear-injected-fault",
        "clear_fault",
        f"fault/{key}",
        destructive=True,
        what=what or "Undo the injected fault. This is the root-cause fix.",
        manual_k8s=k8s or f"uv run --no-project python -m failure_injection recover {key}",
        manual_compose=compose
        or f"FI_BACKEND=docker uv run --no-project python -m failure_injection recover {key}",
        expect=expect or "The workload rolls; the fault toggle returns to its default.",
    )


def _health(service: str, *, name: str = "") -> Step:
    """A read-only health check on one workload.

    The step name carries the target when a runbook checks more than one workload
    (a dependency AND the service itself): step names are the key for per-step
    parameter overrides, per-step approval ids and the UI's list keys, so two steps
    called ``verify-health`` in one runbook are two things the operator cannot tell
    apart — and the executor now refuses a runbook whose step names collide.
    """
    np, cp = PORTS.get(service, (0, 0))
    return Step(
        name or "verify-health",
        "healthcheck",
        f"deployment/{service}",
        what="Read-only check that the service recovered.",
        manual_k8s=f"kubectl -n {NAMESPACE} rollout status deploy/{service} --timeout=120s\n"
        f"curl http://localhost:{np}/health"
        if np
        else f"kubectl -n {NAMESPACE} rollout status deploy/{service} --timeout=120s",
        manual_compose=f"curl http://localhost:{cp}/health" if cp else "",
        expect='HTTP 200 with {"status":"ok"} and every dependency true.',
    )


def _restart_pair(service: str) -> list[Step]:
    """Drain, then restart with a rollback action.

    Two steps rather than one on purpose. The drain is non-destructive so it
    runs autonomously; only the restart hits the HITL gate. And the restart
    declares ``rescale_previous`` as its rollback, which is what makes it a
    *reversible* destructive action — CLAUDE.md non-negotiable #5: every action
    must be reversible, and the reverse must have been tested at least once.
    """
    return [
        Step(
            "drain-connections",
            "drain",
            f"deployment/{service}",
            what="Stop sending new traffic to the pods before they are replaced.",
            manual_k8s=f"kubectl -n {NAMESPACE} annotate pod -l app={service} "
            f"drain=true --overwrite",
            expect="In-flight requests finish; no new work is routed to the old pods.",
        ),
        Step(
            "restart-pods",
            "restart_deployment",
            f"deployment/{service}",
            destructive=True,
            rollback_action="rescale_previous",
            what="Roll the deployment. Requires approval; auto-rolls back if the "
            "new pods do not become healthy.",
            manual_k8s=f"kubectl -n {NAMESPACE} rollout restart deploy/{service}\n"
            f"kubectl -n {NAMESPACE} rollout status deploy/{service} --timeout=120s",
            manual_compose=f"docker compose restart {service}",
            expect="New pods reach Ready; restartCount on the old pods stops mattering.",
        ),
    ]


def _scale_up(workload: str) -> Step:
    return Step("scale-up", "scale_deployment", workload, destructive=True)


RUNBOOKS: list[RB] = [
    # ── user-service ────────────────────────────────────────────────────────
    RB(
        slug="user-service-mysql-down",
        title="user-service — MySQL unavailable",
        service="user-service",
        severity="sev1",
        tags=["database", "mysql", "dependency", "5xx", "login"],
        alert="EcommerceMySQLDown",
        also_alerts=["EcommerceUserLoginFailures"],
        symptom="`mysql_connection_status == 0`; POST /login and /register return HTTP 500; "
        "logs show `database connection failed` / connection refused to mysql:3306.",
        cause="The MySQL StatefulSet is scaled to zero, so the SQLAlchemy engine cannot "
        "open a connection. user-service stays up and keeps serving 500s — it does not "
        "crashloop, because /health returns 200 with status=degraded by design.",
        steps=[
            _clear("user_service.mysql_down"),
            Step(
                "wait-for-datastore",
                "healthcheck",
                "statefulset/mysql",
                what="Wait for MySQL to accept connections before checking the app. "
                "The app pod is NOT restarted by recovery, so it may still be "
                "serving errors from its stale connection pool for a few seconds.",
                manual_k8s="kubectl -n ecommerce rollout status statefulset/mysql --timeout=180s",
                manual_compose="docker compose ps mysql",
                expect="1/1 READY; the mysqladmin ping readiness probe passes.",
            ),
            _health("user-service"),
        ],
        notes=[
            "Recovery scales the StatefulSet back to 1 and waits for the rollout, so the "
            "first login after this runbook should already succeed.",
            "The PVC is retained across a scale-to-zero, so no user data is lost.",
        ],
        diagnose=[
            (
                "Is the MySQL StatefulSet scaled down?",
                "kubectl -n ecommerce get statefulset mysql",
                "READY 0/1. If it reads 1/1, this is NOT the fault - check user-service-crashloop instead.",
            ),
            (
                "Is user-service running (not crashlooping)?",
                "kubectl -n ecommerce get pods -l app=user-service",
                "Running with restartCount 0. A climbing restartCount means crashloop, a different runbook.",
            ),
            (
                "What does the service itself say?",
                "curl http://localhost:30081/health",
                '{"status":"degraded","mysql":false} - HTTP 200 by design, so a DB outage does not restart the pod.',
            ),
        ],
        verify=[
            (
                "Connection gauge is back up (PromQL at http://localhost:9090)",
                "mysql_connection_status",
                "1",
            ),
            (
                "End-to-end: register then log in",
                'curl -X POST http://localhost:30081/register -H "Content-Type: application/json" -d \'{"name":"t","email":"t1@example.com","password":"hunter2pass"}\'',
                "HTTP 201 with an id.",
            ),
        ],
        if_stuck=[
            "Check the PVC still binds: `kubectl -n ecommerce get pvc data-mysql-0` - it should be Bound.",
            "If MySQL is Running but the gauge stays 0, the credentials drifted. Compare MYSQL_USER/MYSQL_PASSWORD in the `ecommerce-secrets` Secret against what the StatefulSet was initialised with - the password is only applied on FIRST boot with an empty PVC.",
            "Tail the datastore: `kubectl -n ecommerce logs statefulset/mysql --tail=50`.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="user-service-high-latency",
        title="user-service — high login latency",
        service="user-service",
        severity="sev2",
        tags=["latency", "slow", "login", "p95"],
        alert="EcommerceOrderLatencyHigh",
        symptom="p95 of `order_latency_seconds` above 2s; /login is slow but returns 200.",
        cause="`INJECT_LATENCY_SECONDS` is set on the user-service Deployment, adding a "
        "fixed sleep to /login. Because order-service validates every order against "
        "/profile, the latency surfaces on the ORDER path too — which is why the alert "
        "that fires is EcommerceOrderLatencyHigh rather than a user-service alert.",
        steps=[
            _clear("user_service.high_latency"),
            _health("user-service"),
        ],
        notes=[
            "This alert can also be caused by user_service.high_cpu or "
            "payment_service.high_cpu — check which fault is actually active before "
            "assuming latency injection.",
        ],
        diagnose=[
            (
                "Is the latency toggle set?",
                "kubectl -n ecommerce get deploy user-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_LATENCY_SECONDS\")].value}'",
                "Non-zero (healthy default is 0).",
            ),
            (
                "Is it latency rather than errors?",
                "histogram_quantile(0.95, sum by (le) (rate(order_latency_seconds_bucket[2m])))",
                "Above 2s while orders still return 201.",
            ),
        ],
        verify=[
            (
                "p95 drops back under threshold",
                "histogram_quantile(0.95, sum by (le) (rate(order_latency_seconds_bucket[2m])))",
                "Below 2s.",
            ),
        ],
        if_stuck=[
            "The same alert fires for user_service.high_cpu and payment_service.high_cpu. Check which toggle is actually set before assuming latency injection.",
            "The latency is on /login, but order-service validates every order against /profile - which is why it shows up on the ORDER latency histogram.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        # alert corrected from EcommerceOrderLatencyHigh: that rule watches
        # order-service p95, which user-service CPU does not move. Same fix
        # demo/ui/scenario_provider.py::ALERTNAMES already made.
        slug="user-service-high-cpu",
        title="user-service — CPU saturation",
        service="user-service",
        severity="sev2",
        tags=["cpu", "saturation", "latency", "throttling"],
        alert="EcommerceUserServiceCPUHigh",
        symptom="Container CPU pinned at its limit; request latency climbs across every "
        "user-service endpoint.",
        cause="`INJECT_CPU_LOAD=true` runs a busy loop in the request path. The pod is "
        "CPU-throttled against its 1000m limit rather than crashing, so it degrades "
        "instead of failing over.",
        steps=[
            _clear("user_service.high_cpu"),
            _health("user-service"),
        ],
        notes=[
            "Restarting the pod also clears the symptom, but the toggle is read from the "
            "environment at request time — a restart without clearing the fault brings the "
            "busy loop straight back.",
        ],
        diagnose=[
            (
                "Is the CPU toggle on?",
                "kubectl -n ecommerce get deploy user-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_CPU_LOAD\")].value}'",
                "true",
            ),
            (
                "Is the pod throttled rather than crashing?",
                "kubectl -n ecommerce top pod -l app=user-service",
                "CPU pinned near the 1000m limit; pod stays Running.",
            ),
        ],
        verify=[
            (
                "CPU returns to idle",
                "kubectl -n ecommerce top pod -l app=user-service",
                "CPU well below the limit.",
            ),
        ],
        if_stuck=[
            "A restart clears the symptom but the toggle is read per-request - without clearing it the busy loop returns immediately.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="user-service-crashloop",
        title="user-service — CrashLoopBackOff on startup",
        service="user-service",
        severity="sev1",
        tags=["crashloop", "startup", "config", "database"],
        alert="EcommerceServiceDown",
        symptom='Pod cycling through CrashLoopBackOff; `up{namespace="ecommerce"} == 0` '
        "for user-service; restartCount climbing.",
        cause="`MYSQL_HOST` points at a host that does not resolve. mysql_client.py reads it "
        "with `os.environ[...]` at import time, so the process raises before uvicorn "
        "binds — the container exits and Kubernetes backs off.",
        steps=[
            _clear("user_service.crashloop"),
            _health("user-service"),
        ],
        notes=[
            "A restart alone will NOT fix this: the bad value lives in the pod spec, so "
            "every new pod inherits it. The env var has to be corrected first.",
            "Distinguish from mysql_down: there MySQL is missing but the host resolves, so "
            "the service starts and serves 500s instead of crashlooping.",
        ],
        diagnose=[
            (
                "Is the pod actually crashlooping?",
                "kubectl -n ecommerce get pods -l app=user-service",
                "CrashLoopBackOff with a climbing RESTARTS count.",
            ),
            (
                "Why did the container exit?",
                "kubectl -n ecommerce describe pod -l app=user-service | Select-String -Pattern 'Reason|Exit Code' ",
                "Reason: Error (NOT OOMKilled - that is the memory-leak runbook).",
            ),
            (
                "What is MYSQL_HOST set to?",
                "kubectl -n ecommerce get deploy user-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"MYSQL_HOST\")].value}'",
                "A host that does not resolve, e.g. nonexistent-db-host. Healthy value is `mysql`.",
            ),
        ],
        verify=[
            (
                "Pod is Ready and stable",
                "kubectl -n ecommerce get pods -l app=user-service",
                "1/1 Running; RESTARTS stops climbing.",
            ),
            (
                "Prometheus can scrape it again",
                'up{namespace="ecommerce",service_name="ecommerce-user-service"}',
                "1",
            ),
        ],
        if_stuck=[
            "A restart alone will NOT help: the bad value lives in the pod spec, so every new pod inherits it. The env var must be corrected first.",
            "If MYSQL_HOST is already `mysql` and it still crashloops, the container is failing for another reason - read the logs of the PREVIOUS attempt: `kubectl -n ecommerce logs deploy/user-service --previous`.",
            "Re-apply the manifest to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    # Two runbooks for one fault, on purpose. Releasing the held sessions is the
    # root-cause fix and touches nothing; it is the right first move. But MySQL was
    # refusing connections while user-service kept trying, so the app's own pool can
    # be left holding sockets that are open locally and dead server-side — SQLAlchemy
    # only discovers that on the next checkout. When logins still fail after the
    # sessions are released, the second runbook adds the recycle. Same fault, two
    # blast radii: the executor offers both and the SRE picks (§6 CASE 2).
    RB(
        slug="user-service-pool-exhaustion",
        title="user-service — MySQL connection pool exhausted (release held sessions)",
        service="user-service",
        severity="sev1",
        tags=["database", "connections", "pool", "saturation", "login"],
        alert="EcommerceUserLoginFailures",
        symptom='`login_failure_total{reason="db_error"}` climbing; POST /login returns 500; '
        "`mysql_connection_status` flapping between 1 and 0. MySQL itself is Running and "
        "READY 1/1 the whole time.",
        cause="An external client holds ~155 sessions open against MySQL, whose "
        "`max_connections` is 151. The server refuses every NEW connection with 'Too many "
        "connections'. user-service is a bystander: its own SQLAlchemy pool is healthy, it "
        "simply cannot open anything new. This is the production shape of the failure — some "
        "other client exhausts the server and an innocent service starts failing.",
        steps=[
            _clear(
                "user_service.pool_exhaustion",
                what="Release the externally held MySQL sessions. This is the "
                "root-cause fix, and it does NOT touch user-service — the app was "
                "never the broken party.",
                expect="MySQL stops refusing new connections; user-service can open "
                "sessions again without being restarted.",
            ),
            Step(
                "verify-datastore-accepts-connections",
                "healthcheck",
                "statefulset/mysql",
                what="Confirm MySQL is accepting new connections again. Unlike the "
                "mysql_down runbook this is NOT waiting for a rollout — MySQL never "
                "went down, it was only refusing new sessions.",
                manual_k8s="kubectl -n ecommerce get statefulset mysql\n"
                "kubectl -n ecommerce logs deploy/user-service --tail=20",
                expect="mysql still READY 1/1, and no further 'Too many connections' "
                "lines appear in the user-service log.",
            ),
            _health("user-service"),
        ],
        notes=[
            "Recovery releases the external sessions WITHOUT restarting user-service — which "
            "is the point: the app was never the problem, so it is not the thing to restart.",
            "If /login still fails once the sessions are released, the app's pool is holding "
            "dead sockets. Use user-service-pool-exhaustion-recycle for that.",
            "MySQL's own `max_connections` is not raised as part of recovery. Raising it "
            "would only move the ceiling — the external client would exhaust the new one too.",
        ],
        diagnose=[
            (
                "Is MySQL itself healthy? (this is what separates it from mysql_down)",
                "kubectl -n ecommerce get statefulset mysql",
                "READY 1/1. A 0/1 here means the StatefulSet is scaled down - that is "
                "user-service-mysql-down, not pool exhaustion.",
            ),
            (
                "Which login failure reason is climbing?",
                "sum by (reason) (login_failure_total)",
                'reason="db_error" climbing. reason="invalid_credentials" alone is NOT a '
                "fault - every load generator posts a bogus password by design, which is "
                "exactly why the alert rule pins the reason.",
            ),
            (
                "Does the app report a server-side refusal rather than its own pool filling?",
                "kubectl -n ecommerce logs deploy/user-service --tail=30",
                "'Too many connections' raised on connect. A 'QueuePool limit ... overflow' "
                "message instead would mean the APP's pool is the bottleneck, not the server's.",
            ),
        ],
        verify=[
            (
                "Database-backed login failures stop (PromQL at http://localhost:9090)",
                'sum(rate(login_failure_total{reason="db_error"}[2m]))',
                "0.",
            ),
            (
                "A fresh connection succeeds end to end",
                'curl -X POST http://localhost:30081/register -H "Content-Type: application/json" -d \'{"name":"t","email":"pool1@example.com","password":"hunter2pass"}\'',
                "HTTP 201 with an id.",
            ),
        ],
        if_stuck=[
            "Recovery does NOT restart user-service, so a pool still holding sockets that died server-side will keep failing. That is what user-service-pool-exhaustion-recycle is for.",
            "Do not raise MySQL's max_connections to 'fix' this. The external client exhausts whatever ceiling exists; the fix is to stop the client holding the sessions.",
            "This fault leaves MySQL READY 1/1 throughout. If MySQL is 0/1 you are in user-service-mysql-down and this runbook will not help.",
            "The injected holder self-expires after 600s, so a fault left alone appears to 'fix itself' - do not read that as a successful remediation.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
        ],
    ),
    RB(
        slug="user-service-pool-exhaustion-recycle",
        title="user-service — MySQL connection pool exhausted (release, then recycle pods)",
        service="user-service",
        severity="sev1",
        tags=["database", "connections", "pool", "recycle", "stale"],
        alert="EcommerceUserLoginFailures",
        symptom='`login_failure_total{reason="db_error"}` still climbing AFTER the held '
        "sessions were released; MySQL READY 1/1 and accepting connections, but /login "
        "keeps returning 500.",
        cause="Same root cause as user-service-pool-exhaustion — an external client "
        "exhausted MySQL's 151 `max_connections`. The residual symptom is secondary: "
        "user-service's SQLAlchemy pool is holding connections that are open locally and "
        "already dead server-side, and it only discovers that on the next checkout.",
        steps=[
            _clear(
                "user_service.pool_exhaustion",
                what="Release the externally held MySQL sessions. This is the "
                "root-cause fix, and it does NOT touch user-service — the app was "
                "never the broken party.",
                expect="MySQL stops refusing new connections; user-service can open "
                "sessions again without being restarted.",
            ),
            Step(
                "verify-datastore-accepts-connections",
                "healthcheck",
                "statefulset/mysql",
                what="Confirm the server has headroom again BEFORE recycling. Restarting "
                "into a still-exhausted server just moves the failure to the new pods.",
                manual_k8s="kubectl -n ecommerce get statefulset mysql",
                expect="mysql READY 1/1.",
            ),
            *_restart_pair("user-service"),
            _health("user-service"),
        ],
        notes=[
            "Prefer user-service-pool-exhaustion first: it fixes the root cause and touches "
            "no workload. Reach for this one only when logins still fail after the sessions "
            "were released.",
            "The restart is gated and reversible (`rescale_previous`), so an unhealthy "
            "rollout is undone rather than left in place.",
            "Recycling WITHOUT clearing the fault first is useless — the new pods meet the "
            "same exhausted server. That is why clear_fault is step 1 here, not the restart.",
        ],
        diagnose=[
            (
                "Have the held sessions actually been released yet?",
                "uv run --no-project python -m failure_injection list",
                "user_service.pool_exhaustion NOT listed as active. If it is still active, "
                "run user-service-pool-exhaustion first - recycling now achieves nothing.",
            ),
            (
                "Is MySQL healthy but the app still failing?",
                "kubectl -n ecommerce get statefulset mysql ; curl http://localhost:30081/health",
                'mysql READY 1/1 while /health reports {"status":"degraded","mysql":false} - '
                "the split that means the app's own pool is stale.",
            ),
        ],
        verify=[
            (
                "Database-backed login failures stop",
                'sum(rate(login_failure_total{reason="db_error"}[2m]))',
                "0.",
            ),
            (
                "The service reports every dependency healthy",
                "curl http://localhost:30081/health",
                '{"status":"ok","mysql":true}',
            ),
        ],
        if_stuck=[
            "If the recycle brings the pods back and they fail again within seconds, the sessions were never released - re-check `failure_injection list`.",
            "A restart is not a fix for an exhausted server. It only helps the secondary symptom (a stale local pool), which is why this runbook clears the fault first.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
        ],
    ),
    # ── order-service ───────────────────────────────────────────────────────
    RB(
        slug="order-service-postgres-down",
        title="order-service — PostgreSQL unavailable",
        service="order-service",
        severity="sev1",
        tags=["database", "postgres", "dependency", "5xx", "orders"],
        alert="EcommercePostgresDown",
        symptom="`postgres_connection_status == 0`; POST /orders returns 500; "
        '`orders_failed_total{reason="db_error"}` climbing.',
        cause="The Postgres StatefulSet is scaled to zero. Orders fail at the persist step, "
        "before payment is ever called — so no charge is attempted and no money moves.",
        steps=[
            _clear("order_service.postgres_down"),
            Step(
                "wait-for-datastore",
                "healthcheck",
                "statefulset/postgres",
                what="Wait for PostgreSQL to accept connections before checking the app. "
                "The app pod is NOT restarted by recovery, so it may still be "
                "serving errors from its stale connection pool for a few seconds.",
                manual_k8s="kubectl -n ecommerce rollout status statefulset/postgres --timeout=180s",
                manual_compose="docker compose ps postgres",
                expect="1/1 READY; the pg_isready readiness probe passes.",
            ),
            _health("order-service"),
        ],
        notes=[
            "Because the failure happens before the payment call, there are no orphaned "
            "charges to reconcile after recovery.",
        ],
        diagnose=[
            (
                "Is Postgres scaled down?",
                "kubectl -n ecommerce get statefulset postgres",
                "READY 0/1.",
            ),
            (
                "What does order-service report?",
                "curl http://localhost:30082/health",
                '{"status":"degraded","postgres":false}',
            ),
            (
                "Which failure reason?",
                "sum by (reason) (orders_failed_total)",
                'reason="db_error" climbing.',
            ),
        ],
        verify=[
            ("Connection gauge recovers", "postgres_connection_status", "1"),
            (
                "An order persists and reaches PAID",
                "curl http://localhost:30082/health",
                '{"status":"ok","postgres":true}',
            ),
        ],
        if_stuck=[
            "The failure happens BEFORE the payment call, so there are no orphaned charges to reconcile.",
            "Check the PVC: `kubectl -n ecommerce get pvc data-postgres-0` should be Bound.",
            "If Postgres is Running but the gauge stays 0, check PGDATA - the official image refuses to initialise into a non-empty mount unless data lives in a subdirectory.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="order-service-payment-timeout",
        title="order-service — payment call timing out",
        service="order-service",
        severity="sev2",
        tags=["timeout", "dependency", "payment", "gateway"],
        alert="EcommercePaymentTimeouts",
        symptom="`payment_timeout_total` climbing; POST /orders returns 504; orders left "
        "in FAILED state with a row still present in Postgres.",
        cause="The mock payment gateway has `INJECT_DELAY_SECONDS` set above "
        "order-service's `PAYMENT_TIMEOUT_SECONDS` (default 5s), so every charge exceeds "
        "the client timeout.",
        steps=[
            _clear("order_service.payment_timeout"),
            _health("mock-payment-gateway", name="verify-gateway-health"),
            _health("order-service"),
        ],
        notes=[
            "The fault is on mock-payment-gateway, NOT on order-service — the alert names "
            "the victim, not the cause. Same underlying fault as "
            "payment_service.gateway_timeout.",
            "Orders that already failed stay FAILED; the row is kept deliberately so the "
            "customer can see the attempt.",
        ],
        diagnose=[
            (
                "Is the gateway delay set above the client timeout?",
                "kubectl -n ecommerce get deploy mock-payment-gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_DELAY_SECONDS\")].value}'",
                "A value above PAYMENT_TIMEOUT_SECONDS (default 5).",
            ),
            ("Are timeouts counting up?", "payment_timeout_total", "Climbing."),
            (
                "Confirm the fault is NOT on order-service or payment-service",
                "kubectl -n ecommerce get pods -l app=payment-service",
                "Running and healthy - it is blocked on its upstream, not broken.",
            ),
        ],
        verify=[
            ("Timeout counter stops climbing", "rate(payment_timeout_total[2m])", "0"),
            (
                "An order completes and is PAID",
                "curl http://localhost:30082/health",
                '{"status":"ok","postgres":true} and a new order returns status PAID.',
            ),
        ],
        if_stuck=[
            "The fault is on mock-payment-gateway, two hops downstream. The alert names the victim, not the cause - restarting order-service or payment-service changes nothing.",
            "Do NOT raise PAYMENT_TIMEOUT_SECONDS to 'fix' it: that masks a broken dependency by making customers wait longer for the same failure.",
            "Orders that already failed stay FAILED. The row is kept deliberately so the customer can see the attempt.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="order-service-http-500",
        title="order-service — 5xx on order creation",
        service="order-service",
        severity="sev2",
        tags=["error", "5xx", "orders", "application"],
        alert="EcommerceOrderErrorRateHigh",
        symptom='`orders_failed_total{reason="injected_500"}` climbing; every POST /orders '
        "returns 500 immediately, before any dependency is called.",
        cause="`INJECT_HTTP_500=true` on the order-service Deployment forces an unhandled "
        "5xx at the top of the create-order handler.",
        steps=[
            _clear("order_service.http_500"),
            _health("order-service"),
        ],
        notes=[
            "The failure is first in the handler, so user validation, the database write "
            "and the payment call never run. No partial state to clean up.",
            "This alert needs SUSTAINED traffic to fire: it is a rate() over a 2m window, "
            "so a short burst decays to zero before the rule evaluates.",
        ],
        diagnose=[
            (
                "Is the 500 toggle on?",
                "kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_HTTP_500\")].value}'",
                "true",
            ),
            (
                "Which failure reason is counting up?",
                "sum by (reason) (orders_failed_total)",
                'reason="injected_500" climbing. A different reason means a different fault.',
            ),
            (
                "Does it fail before touching any dependency?",
                "kubectl -n ecommerce logs deploy/order-service --tail=20",
                '"injected HTTP 500 on order creation" with no database or payment log lines after it.',
            ),
        ],
        verify=[
            (
                "Error rate returns to zero",
                "sum by (reason) (rate(orders_failed_total[2m]))",
                "0 for injected_500.",
            ),
            (
                "A real order succeeds",
                "See demo/ecommerce/README.md for the register -> login -> order sequence",
                'HTTP 201 with "status":"PAID".',
            ),
        ],
        if_stuck=[
            "This alert needs SUSTAINED traffic to fire and to clear - it is a rate() over 2 minutes, so a short burst decays to zero before the rule evaluates. Drive load with `--load 90` when reproducing.",
            "The failure is the first thing in the handler, so user validation, the DB write and the payment call never run. There is no partial state to clean up.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="order-service-memory-leak",
        title="order-service — memory leak / OOMKilled",
        service="order-service",
        severity="sev1",
        tags=["memory", "oom", "leak", "restart", "resource"],
        alert="EcommerceOrderServiceMemoryHigh",
        symptom="RSS climbing with every order; pod terminated with reason OOMKilled; "
        "restartCount incrementing; scrapes fail during each restart.",
        cause="`INJECT_MEMORY_LEAK=true` appends a 5 MB chunk to a module-global list on "
        "every order and never frees it. The container hits its 256Mi limit and the kernel "
        "OOM-kills it.",
        steps=[
            _clear("order_service.memory_leak_oom"),
            _health("order-service"),
        ],
        notes=[
            "The 256Mi limit is deliberate — it makes the leak reach OOMKilled in "
            "demo-friendly time. Raising it hides the symptom rather than fixing it.",
            "Clearing the fault also resets the limit, because the injector lowers it as "
            "part of the fault.",
            "A restart frees the leaked memory but the leak resumes immediately; clearing "
            "the toggle is the actual fix.",
        ],
        diagnose=[
            (
                "Was the container OOM-killed?",
                "kubectl -n ecommerce get pods -l app=order-service -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}'",
                "OOMKilled. Anything else means a different failure.",
            ),
            (
                "Is the leak toggle on?",
                "kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_MEMORY_LEAK\")].value}'",
                "true",
            ),
            (
                "Does memory track order volume rather than uptime?",
                'container_memory_rss_bytes{namespace="ecommerce",pod=~"order-service.*"}',
                "A sawtooth climbing with traffic and dropping at each restart - a per-request leak, not a slow accumulation.",
            ),
        ],
        verify=[
            (
                "RSS stays flat under sustained order traffic",
                'container_memory_rss_bytes{namespace="ecommerce",pod=~"order-service.*"}',
                "Roughly level instead of climbing.",
            ),
            (
                "No further restarts",
                "kubectl -n ecommerce get pods -l app=order-service",
                "RESTARTS stops incrementing.",
            ),
        ],
        if_stuck=[
            "Do NOT just raise the memory limit. The leak is unbounded, so a bigger limit only lengthens the interval between kills.",
            "A restart frees the leaked memory but the leak resumes immediately - clearing the toggle is the actual fix.",
            "Confirm the limit is back at its manifest value: `kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'` should be 256Mi.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    # Two runbooks again, and the split is sharper here than for the pool: whether the
    # recycle is needed is decided by something the operator can read off the pod. The
    # hog usually dies before the app does (it has the largest RSS, so the cgroup OOM
    # killer picks it), which leaves the container alive and reclaiming — killing the
    # hog is then the whole fix. But if the kernel got the app instead, the pod is
    # cycling and needs the restart path.
    RB(
        slug="order-service-memory-exhaust",
        title="order-service — external memory pressure (release the hog)",
        service="order-service",
        severity="sev1",
        tags=["memory", "exhaustion", "cgroup", "external", "resource"],
        alert="EcommerceOrderServiceMemoryHigh",
        symptom="`container_memory_working_set_bytes` for order-service pinned near its "
        "256Mi limit; the application's own heap metrics look normal; restartCount is "
        "usually NOT incrementing.",
        cause="An external process holds ~200MB resident inside the container's cgroup. The "
        "application is a bystander — its heap is clean, and the pressure comes from a "
        "neighbour in the same cgroup. The kernel reclaims rather than killing, and when it "
        "does kill it picks the hog (largest RSS), so pod-level "
        "`lastState.terminated.reason` is often NOT OOMKilled.",
        steps=[
            _clear(
                "order_service.memory_exhaust",
                what="Kill the external process holding pages resident in the "
                "container's cgroup. This is the root-cause fix.",
                expect="The cgroup's working set falls away from the limit. If the "
                "kernel already OOMKilled the container, this is a no-op — the kernel "
                "got there first.",
            ),
            _health("order-service"),
        ],
        notes=[
            "This is NOT order-service-memory-leak. There the application grows its own heap "
            "(INJECT_MEMORY_LEAK=true) and the app is what gets OOMKilled; here the app is "
            "innocent and the RCA has to come from pod state rather than application logs.",
            "Both faults raise the same alert, because working-set-over-limit cannot tell you "
            "whose memory it is. The diagnose step below is what separates them.",
            "Recovery kills only the hog process. If the cgroup already OOMKilled the "
            "container, clearing is a no-op — the kernel got there first — and you want "
            "order-service-memory-exhaust-recycle instead.",
        ],
        diagnose=[
            (
                "Is the working set actually pinned at the limit?",
                'max by (pod) (container_memory_working_set_bytes{namespace="ecommerce",pod=~"order-service-.*"} / (container_spec_memory_limit_bytes{namespace="ecommerce",pod=~"order-service-.*"} > 0))',
                "Above 0.9. This is the rule's own expression, so it agrees with the alert.",
            ),
            (
                "Is the APPLICATION leaking, or is the pressure external?",
                "kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_MEMORY_LEAK\")].value}'",
                "Empty or false. `true` means the application heap leak - use "
                "order-service-memory-leak, which is a different fix.",
            ),
            (
                "Was the container itself OOMKilled?",
                "kubectl -n ecommerce get pods -l app=order-service -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}'",
                "Usually EMPTY for this fault - the hog dies, not the app. An OOMKilled "
                "container with a climbing restartCount means use the -recycle runbook.",
            ),
        ],
        verify=[
            (
                "Working set falls back below the threshold",
                'max by (pod) (container_memory_working_set_bytes{namespace="ecommerce",pod=~"order-service-.*"} / (container_spec_memory_limit_bytes{namespace="ecommerce",pod=~"order-service-.*"} > 0))',
                "Comfortably under 0.9.",
            ),
            (
                "The pod was never restarted by this remediation",
                "kubectl -n ecommerce get pods -l app=order-service",
                "1/1 Running with RESTARTS unchanged from before the fix.",
            ),
        ],
        if_stuck=[
            "If the container was already OOMKilled, killing the hog is a no-op and the pod needs a clean start - use order-service-memory-exhaust-recycle.",
            "Do NOT raise the 256Mi limit. The pressure is external and unbounded, so a bigger limit only lengthens the interval before it is hit again.",
            "An OOMKilled container plus INJECT_MEMORY_LEAK=true is the application leak, not this fault. Read the env var before choosing.",
            "The injected hog self-expires after 600s, so a fault left alone appears to 'fix itself' - do not read that as a successful remediation.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
        ],
    ),
    RB(
        slug="order-service-memory-exhaust-recycle",
        title="order-service — external memory pressure (release, then recycle pods)",
        service="order-service",
        severity="sev1",
        tags=["memory", "exhaustion", "cgroup", "recycle", "resource"],
        alert="EcommerceOrderServiceMemoryHigh",
        symptom="Working set at the 256Mi limit AND the container has been OOMKilled — "
        "`lastState.terminated.reason=OOMKilled` with restartCount climbing, so the pod is "
        "cycling rather than merely under pressure.",
        cause="Same external memory pressure as order-service-memory-exhaust, but this time "
        "the cgroup OOM killer picked the container's main process rather than the hog. "
        "Releasing the hog stops the pressure; it does not bring a cycling pod back cleanly.",
        steps=[
            _clear(
                "order_service.memory_exhaust",
                what="Kill the external process holding pages resident in the "
                "container's cgroup. This is the root-cause fix.",
                expect="The cgroup's working set falls away from the limit. If the "
                "kernel already OOMKilled the container, this is a no-op — the kernel "
                "got there first.",
            ),
            *_restart_pair("order-service"),
            _health("order-service"),
        ],
        notes=[
            "Prefer order-service-memory-exhaust when the container is still Running: it is "
            "the same fix without a rollout.",
            "clear_fault comes FIRST. Restarting into live memory pressure just OOMKills the "
            "new pod too.",
            "The restart is gated and reversible (`rescale_previous`).",
        ],
        diagnose=[
            (
                "Is the pod actually cycling (which is what justifies the restart)?",
                "kubectl -n ecommerce get pods -l app=order-service",
                "RESTARTS climbing. If it is stable at 1/1, use order-service-memory-exhaust "
                "instead and avoid the rollout.",
            ),
            (
                "Was it OOMKilled rather than erroring?",
                "kubectl -n ecommerce get pods -l app=order-service -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}'",
                "OOMKilled. `Error` instead points at a startup failure, not memory.",
            ),
            (
                "Is the pressure external rather than the application's own heap?",
                "kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_MEMORY_LEAK\")].value}'",
                "Empty or false. `true` is the application leak - order-service-memory-leak.",
            ),
        ],
        verify=[
            (
                "Restarts stop",
                "kubectl -n ecommerce get pods -l app=order-service",
                "1/1 Running and RESTARTS stops incrementing.",
            ),
            (
                "Working set stays flat under sustained order traffic",
                'max by (pod) (container_memory_working_set_bytes{namespace="ecommerce",pod=~"order-service-.*"} / (container_spec_memory_limit_bytes{namespace="ecommerce",pod=~"order-service-.*"} > 0))',
                "Level and well under 0.9.",
            ),
        ],
        if_stuck=[
            "If the new pod is OOMKilled again within seconds, the hog was not released - check `failure_injection list` before restarting a third time.",
            "Do NOT raise the memory limit as a workaround; the external pressure is unbounded.",
            "Confirm the limit is back at its manifest value: `kubectl -n ecommerce get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'` should be 256Mi.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
        ],
    ),
    RB(
        slug="order-service-packet-loss",
        title="order-service — network packet loss",
        service="order-service",
        severity="sev2",
        tags=["network", "packet-loss", "tcp", "retransmits"],
        # Deliberately no alert: no Prometheus rule covers packet loss on this
        # cluster, so declaring one would be a constraint no incident can satisfy.
        alert="",
        symptom="Order creation intermittently fails with connection errors under load; TCP "
        "retransmits climbing. No dedicated alert fires — 5% loss degrades rather than "
        "breaks, and it may surface only as raised latency on the existing order rules.",
        cause="5% packet loss applied to the order-service pod's network interface with "
        "`tc netem`. Note this fault frequently CANNOT be injected on this cluster at all: "
        "the app images ship without `iproute2` and the pods do not hold CAP_NET_ADMIN, so "
        "the injector has nothing to drive.",
        steps=[
            _clear(
                "order_service.packet_loss",
                what="Remove the netem qdisc from the pod's interface.",
                expect="The qdisc returns to the pod default and packets stop being "
                "dropped. A no-op if the loss was never applied — which on this "
                "cluster is the usual case.",
            ),
            _health("order-service"),
        ],
        notes=[
            "There is deliberately no Prometheus rule for packet loss, so do not wait for an "
            "alert to clear as your signal that this is fixed.",
            "Verify the fault can even exist here before spending time on it — see the first "
            "diagnose step. On this cluster the usual answer is that it cannot.",
        ],
        diagnose=[
            (
                "Can this fault even be applied on this cluster?",
                "kubectl -n ecommerce exec deploy/order-service -- tc qdisc show dev eth0",
                "A netem qdisc with `loss 5%`. If tc is 'not found' or the call is denied, "
                "the injector could never have applied it (no iproute2, no CAP_NET_ADMIN) - "
                "packet loss is NOT what you are looking at, so stop here.",
            ),
            (
                "Are orders failing on the network rather than on a dependency?",
                "sum by (reason) (orders_failed_total)",
                'A connection/network reason climbing rather than reason="injected_500" or '
                '"db_error", both of which point at different runbooks.',
            ),
        ],
        verify=[
            (
                "The qdisc is back to the pod default",
                "kubectl -n ecommerce exec deploy/order-service -- tc qdisc show dev eth0",
                "No netem entry.",
            ),
            (
                "Orders succeed consistently under load",
                "See demo/ecommerce/README.md for the register -> login -> order sequence",
                'HTTP 201 with "status":"PAID", repeatably.',
            ),
        ],
        if_stuck=[
            "On this cluster the injector usually cannot apply packet loss at all (no iproute2 in the image, no CAP_NET_ADMIN on the pod). If tc is unavailable, this fault is not active and you are chasing the wrong runbook.",
            "If you are chasing a REAL network problem rather than an injected one, this runbook does not apply - look at the CNI and node-level interface counters instead.",
            "5% loss degrades rather than breaks. Expect intermittent failures and raised latency, not a clean outage.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
        ],
    ),
    # ── payment-service ─────────────────────────────────────────────────────
    RB(
        slug="payment-service-redis-down",
        title="payment-service — Redis unavailable",
        service="payment-service",
        severity="sev1",
        tags=["cache", "redis", "dependency", "5xx", "payments"],
        alert="EcommerceRedisDown",
        symptom="`redis_connection_status == 0`; POST /payments returns 500; logs show "
        "`redis connection error`.",
        cause="The Redis StatefulSet is scaled to zero. Redis is the payment record store "
        "here, not a cache — payments cannot be persisted, so the charge is rejected rather "
        "than silently unrecorded.",
        steps=[
            _clear("payment_service.redis_down"),
            Step(
                "wait-for-datastore",
                "healthcheck",
                "statefulset/redis",
                what="Wait for Redis to accept connections before checking the app. "
                "The app pod is NOT restarted by recovery, so it may still be "
                "serving errors from its stale connection pool for a few seconds.",
                manual_k8s="kubectl -n ecommerce rollout status statefulset/redis --timeout=180s",
                manual_compose="docker compose ps redis",
                expect="1/1 READY; the redis-cli ping readiness probe passes.",
            ),
            _health("payment-service"),
        ],
        notes=[
            "Redis here is a system of record, not a cache — do NOT treat this as a "
            "cache-miss degradation. There is no fallback path.",
            "Payment records written before the outage survive: the PVC is retained.",
        ],
        diagnose=[
            ("Is Redis scaled down?", "kubectl -n ecommerce get statefulset redis", "READY 0/1."),
            (
                "What does payment-service report?",
                "curl http://localhost:30083/health",
                '{"status":"degraded","redis":false}',
            ),
            (
                "Are payments failing rather than hanging?",
                "kubectl -n ecommerce logs deploy/payment-service --tail=20",
                '"redis connection error" - socket timeouts are capped at 2s so a dead Redis fails fast.',
            ),
        ],
        verify=[
            ("Connection gauge recovers", "redis_connection_status", "1"),
            (
                "A payment record is written",
                "kubectl -n ecommerce exec statefulset/redis -- redis-cli KEYS 'payment:*'",
                "At least one key after a successful order.",
            ),
        ],
        if_stuck=[
            "Redis here is the payment SYSTEM OF RECORD, not a cache. Do not 'degrade gracefully' past it - that would take money without recording it.",
            "Payment records written before the outage survive: the PVC is retained across a scale-to-zero.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="payment-service-gateway-timeout",
        title="payment-service — external gateway timing out",
        service="payment-service",
        severity="sev2",
        tags=["timeout", "gateway", "dependency", "external"],
        alert="EcommercePaymentTimeouts",
        symptom="POST /payments hangs then fails; upstream order-service reports 504.",
        cause="`INJECT_DELAY_SECONDS` on mock-payment-gateway exceeds payment-service's "
        "`GATEWAY_TIMEOUT_SECONDS`, so the outbound charge call times out.",
        steps=[
            _clear("payment_service.gateway_timeout"),
            _health("mock-payment-gateway", name="verify-gateway-health"),
            _health("payment-service"),
        ],
        notes=[
            "The fault is on the gateway, not on payment-service. Restarting "
            "payment-service changes nothing.",
        ],
        diagnose=[
            (
                "Is the gateway delay above payment-service's timeout?",
                "kubectl -n ecommerce get deploy mock-payment-gateway -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_DELAY_SECONDS\")].value}'",
                "A value above GATEWAY_TIMEOUT_SECONDS (default 5).",
            ),
        ],
        verify=[
            (
                "A charge completes within the timeout",
                "curl http://localhost:30083/health",
                "HTTP 200 and a new order reaches PAID.",
            ),
        ],
        if_stuck=[
            "Restarting payment-service changes nothing - the fault is on the gateway it calls.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="payment-service-high-cpu",
        title="payment-service — CPU saturation",
        service="payment-service",
        severity="sev2",
        tags=["cpu", "saturation", "latency", "throttling"],
        alert="EcommercePaymentServiceCPUHigh",
        symptom="payment-service CPU at its limit; charge latency climbing; order p95 "
        "crosses 2s because orders block on payment.",
        cause="`INJECT_CPU_LOAD=true` runs a busy loop in the charge path; the pod is "
        "throttled against its 1000m limit.",
        steps=[
            _clear("payment_service.high_cpu"),
            _health("payment-service"),
        ],
        diagnose=[
            (
                "Is the CPU toggle on?",
                "kubectl -n ecommerce get deploy payment-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_CPU_LOAD\")].value}'",
                "true",
            ),
        ],
        verify=[
            (
                "CPU returns to idle",
                "kubectl -n ecommerce top pod -l app=payment-service",
                "CPU well below the limit.",
            ),
        ],
        if_stuck=[
            "Orders block on payment, so order p95 recovers only after payment-service does.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    RB(
        slug="payment-service-http-500",
        title="payment-service — 5xx on charge",
        service="payment-service",
        severity="sev2",
        tags=["error", "5xx", "payments", "application"],
        alert="EcommerceOrderErrorRateHigh",
        symptom="POST /payments returns 500; order-service marks orders FAILED with "
        '`orders_failed_total{reason="payment_failed"}`.',
        cause="`INJECT_HTTP_500=true` on the payment-service Deployment forces a 5xx on "
        "every charge.",
        steps=[
            _clear("payment_service.http_500"),
            _health("payment-service"),
        ],
        notes=[
            "Orders reach PENDING and are then marked FAILED — the order row survives so "
            "the failed attempt stays visible to the customer.",
        ],
        diagnose=[
            (
                "Is the 500 toggle on?",
                "kubectl -n ecommerce get deploy payment-service -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name==\"INJECT_HTTP_500\")].value}'",
                "true",
            ),
            (
                "How does it surface upstream?",
                "sum by (reason) (orders_failed_total)",
                'reason="payment_failed" climbing - order-service reports the downstream failure.',
            ),
        ],
        verify=[
            (
                "Charges succeed again",
                "curl http://localhost:30083/health",
                '{"status":"ok","redis":true} and a new order reaches PAID.',
            ),
        ],
        if_stuck=[
            "Orders reach PENDING then FAILED. The order row survives so the failed attempt stays visible to the customer.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
            "Re-apply the manifests to reset the whole spec: `kubectl apply -f demo/ecommerce/k8s/20-app.yaml`.",
        ],
    ),
    # One runbook, not two: recovery for this fault IS a pod restart (resolv.conf is
    # written by the kubelet at pod start, so nothing short of a new pod restores it).
    # A separate "restart" variant would be the same procedure under another name.
    # What this fault needs instead is a diagnose section that stops an SRE from
    # remediating the wrong dependency — see step 1 below.
    RB(
        slug="payment-service-dns-failure",
        title="payment-service — DNS resolution broken",
        service="payment-service",
        severity="sev1",
        tags=["dns", "resolution", "network", "gateway", "dependency"],
        alert="EcommercePaymentGatewayUnreachable",
        # Broken DNS raises this too, pointing at a healthy Redis. Declared so an
        # incident opened off the Redis alert still offers this runbook instead of
        # silently filtering it out — see the also_alerts note on RB.
        also_alerts=["EcommerceRedisDown"],
        symptom='`payment_failures_total{reason="gateway_error"}` climbing; `getaddrinfo` '
        "failures in the logs. **`EcommerceRedisDown` also fires, with Redis perfectly "
        "healthy** — that pair is the fingerprint, not two separate incidents.",
        cause="`/etc/resolv.conf` on the payment-service pod is poisoned, so no name "
        "resolves — not the payment gateway, and not Redis either. payment-service re-pings "
        "Redis inside its own `/metrics` handler and the gauge zeroes on ANY exception, so a "
        "name-resolution failure drives `redis_connection_status` to 0 and raises a Redis "
        "alert that points at a healthy datastore.",
        steps=[
            _clear(
                "payment_service.dns_failure",
                what="Replace the pod so the kubelet writes a clean /etc/resolv.conf. "
                "There is no in-place repair: the file is generated at pod start, so a "
                "new pod is the only fix.",
                expect="A new pod reaches Ready with a working resolver; name lookups "
                "succeed and the misleading Redis alert clears on its own.",
            ),
            _health("payment-service"),
        ],
        notes=[
            "Recovery restarts the pod, because that is the only thing that restores "
            "resolv.conf — the kubelet writes it when the pod starts. There is no in-place fix.",
            "Do NOT act on the accompanying EcommerceRedisDown alert. Scaling or restarting "
            "Redis fixes nothing and takes a healthy datastore down for no reason.",
            "This is distinct from payment_service.gateway_timeout: there the gateway answers "
            "too slowly (reason=gateway_timeout), here it cannot be reached at all "
            "(reason=gateway_error).",
        ],
        diagnose=[
            (
                "Is Redis actually down, or just unresolvable? (do this FIRST)",
                "kubectl -n ecommerce get statefulset redis",
                "READY 1/1. Redis healthy while EcommerceRedisDown is firing IS the DNS "
                "fingerprint. A genuine 0/1 means payment-service-redis-down instead.",
            ),
            (
                "Which payment failure reason is climbing?",
                "sum by (reason) (payment_failures_total)",
                'reason="gateway_error" - a connection or name-resolution failure. '
                'reason="gateway_timeout" is a different fault '
                "(payment-service-gateway-timeout).",
            ),
            (
                "Can the pod resolve anything at all?",
                "kubectl -n ecommerce exec deploy/payment-service -- getent hosts redis",
                "No output and a non-zero exit. A healthy pod prints an IP. Also check "
                "`kubectl -n ecommerce exec deploy/payment-service -- cat /etc/resolv.conf`.",
            ),
        ],
        verify=[
            (
                "Gateway connection failures stop",
                'sum(rate(payment_failures_total{reason="gateway_error"}[2m]))',
                "0.",
            ),
            (
                "The misleading Redis signal clears too, without touching Redis",
                "redis_connection_status",
                "1 - because the name resolves again, not because Redis changed.",
            ),
            (
                "The service reports every dependency healthy",
                "curl http://localhost:30083/health",
                '{"status":"ok","redis":true}',
            ),
        ],
        if_stuck=[
            "If EcommerceRedisDown persists AFTER the restart and Redis is still 1/1, re-check resolution from inside the new pod - a surviving alert with a healthy datastore is still a DNS symptom, not a Redis one.",
            "If Redis is genuinely 0/1, you are in payment-service-redis-down and this runbook does not apply.",
            "There is no in-place repair for resolv.conf: the kubelet writes it at pod start, so the pod must be replaced.",
            "Confirm which deployment you are actually looking at. Kubernetes serves the app on :30080 and Docker Compose on :3000 - only the Kubernetes one is scraped by Prometheus, so a fault cleared in one will not change the other.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
        ],
    ),
    RB(
        slug="payment-service-disk-full",
        title="payment-service — disk pressure from a large file",
        service="payment-service",
        severity="sev2",
        tags=["disk", "storage", "filesystem", "enospc"],
        # Deliberately no alert. See the FAULT_FACETS note: a 256MB write to the
        # node's ~1TB shared overlay is 0.025% and undetectable at any threshold
        # that is not itself noise, so no rule was written rather than a fake one.
        alert="",
        symptom="A large file present under `/tmp` on the payment-service pod; application "
        "write paths failing with ENOSPC if the filesystem is genuinely full. **No alert "
        "fires for this fault**, so it is found by looking, not by being paged.",
        cause="A 256MB file was written to `/tmp`, which on this cluster is the containerd "
        "overlay — the node's ~1TB filesystem, shared with etcd and every other pod. The "
        "write is real; the percentage it moves is not measurable.",
        steps=[
            _clear(
                "payment_service.disk_full",
                what="Delete the injected fill file. Scoped to exactly that file — "
                "nothing else under /tmp is touched, because on this cluster /tmp is "
                "the node's shared overlay.",
                expect="The file is gone and no workload is restarted.",
            ),
            _health("payment-service"),
        ],
        notes=[
            "There is deliberately NO Prometheus rule for this fault. Absence of a disk alert "
            "is not evidence the fault is absent — check the filesystem directly.",
            "A percentage-based fill was rejected on purpose: `/` here is the node's "
            "filesystem, so filling it to 95% would be a cluster-wide outage rather than a "
            "service-level scenario.",
            "Making this produce a real DiskPressure signal needs an emptyDir with a "
            "sizeLimit mounted into the pod — see demo/ecommerce/k8s/20-app.yaml.",
        ],
        diagnose=[
            (
                "Is the fill file present? (this is the actual signal)",
                "kubectl -n ecommerce exec deploy/payment-service -- ls -lh /tmp",
                "A file of roughly 256MB. Its absence means this fault is not active.",
            ),
            (
                "How much space does the filesystem report?",
                "kubectl -n ecommerce exec deploy/payment-service -- df -h /tmp",
                "Barely moved. /tmp is the node overlay (~1TB), so 256MB is ~0.025% - do "
                "NOT conclude from a healthy df that there is no fill file.",
            ),
            (
                "Are writes actually failing?",
                "kubectl -n ecommerce logs deploy/payment-service --tail=30",
                "ENOSPC / 'No space left on device' on a write path. On this cluster there is "
                "usually plenty of headroom, so this is often clean even with the fault active.",
            ),
        ],
        verify=[
            (
                "The fill file is gone",
                "kubectl -n ecommerce exec deploy/payment-service -- ls -lh /tmp",
                "No large file remains.",
            ),
            (
                "Charges succeed",
                "curl http://localhost:30083/health",
                '{"status":"ok","redis":true} and a new order reaches PAID.',
            ),
        ],
        if_stuck=[
            "Do not wait for an alert to clear: none exists for this fault by design. The file's absence is the verification.",
            "If df shows the node filesystem genuinely near full, that is a CLUSTER problem, not this scenario - a 256MB scenario file cannot cause it. Investigate node disk usage before deleting anything else.",
            "Never widen the cleanup beyond the injected file. /tmp on this pod is the node's shared overlay, so deleting unfamiliar paths there can affect other workloads.",
            "List what is currently injected: `uv run --no-project python -m failure_injection list` from demo/ecommerce.",
        ],
    ),
    # ── generic per-service recovery ────────────────────────────────────────
    # Matched when an incident's tags do not point at a specific injected fault.
    # Deliberately simple: a restart is the safe first move for an unexplained
    # degradation, and it is reversible.
    RB(
        slug="user-service-restart",
        title="user-service — restart (generic recovery)",
        service="user-service",
        severity="sev3",
        tags=["restart", "generic", "unknown"],
        alert="EcommerceServiceDown",
        generic=True,
        symptom="user-service degraded with no identified injected fault.",
        cause="Unknown. Use when the specific fault runbooks do not match.",
        steps=[*_restart_pair("user-service"), _health("user-service")],
        notes=[
            "If the symptom returns after the restart, an injected fault is still set — "
            "check the scenario catalog before restarting again."
        ],
    ),
    RB(
        slug="order-service-restart",
        title="order-service — restart (generic recovery)",
        service="order-service",
        severity="sev3",
        tags=["restart", "generic", "unknown"],
        alert="EcommerceServiceDown",
        generic=True,
        symptom="order-service degraded with no identified injected fault.",
        cause="Unknown. Use when the specific fault runbooks do not match.",
        steps=[*_restart_pair("order-service"), _health("order-service")],
        notes=[
            "If the symptom returns after the restart, an injected fault is still set — "
            "check the scenario catalog before restarting again."
        ],
    ),
    RB(
        slug="payment-service-restart",
        title="payment-service — restart (generic recovery)",
        service="payment-service",
        severity="sev3",
        tags=["restart", "generic", "unknown"],
        alert="EcommerceServiceDown",
        generic=True,
        symptom="payment-service degraded with no identified injected fault.",
        cause="Unknown. Use when the specific fault runbooks do not match.",
        steps=[*_restart_pair("payment-service"), _health("payment-service")],
        notes=[
            "If the symptom returns after the restart, an injected fault is still set — "
            "check the scenario catalog before restarting again."
        ],
    ),
]


def fault_key(rb: RB) -> str:
    """The failure key this runbook clears, or ``""`` for a generic recovery."""
    for st in rb.steps:
        if st.action == "clear_fault" and st.target.startswith("fault/"):
            return st.target.split("/", 1)[1]
    return ""


def facets(rb: RB) -> tuple[list[str], str, list[str]]:
    """(alerts, failure_category, required_signals) for this runbook.

    Generic recovery runbooks get none of the three: an unconstrained scope is what
    makes them a fallback candidate for any incident on their service rather than a
    runbook that only matches when a specific alert fires.
    """
    if rb.generic:
        return [], "", []
    key = fault_key(rb)
    if not key:
        return ([rb.alert] if rb.alert else []), "", []
    alert, category, signals = FAULT_FACETS[key]
    # An empty alertname is a real state, not a missing value: two faults have no
    # Prometheus rule at all. Declaring ``alerts: [""]`` would be a constraint no
    # incident can ever satisfy, so the alert facet is left unconstrained and the
    # category plus the signals carry the matching.
    alerts = ([alert] if alert else []) + [a for a in rb.also_alerts if a != alert]
    return alerts, category, list(signals)


def target_scope(rb: RB) -> tuple[list[str], list[str]]:
    """(allowed_services, allowed_namespaces) derived from the steps themselves.

    Emitted as an explicit declaration rather than inferred at run time, so parameter
    validation has something to check a step (or a runtime-supplied override) against.
    A datastore runbook legitimately touches a second workload — user-service's MySQL
    runbook waits on ``statefulset/mysql`` — and that is exactly the cross-service
    reach §12 requires the runbook to declare out loud.
    """
    services = {rb.service}
    namespaces = {NAMESPACE}
    for st in rb.steps:
        kind, _, name = st.target.partition("/")
        if not name:
            continue
        if kind == "fault":
            # fault/<service_with_underscores>.<failure> — the service half only.
            services.add(name.split(".", 1)[0].replace("_", "-"))
        else:  # deployment/<name>, statefulset/<name>
            services.add(name)
    return sorted(services), sorted(namespaces)


def prerequisites(rb: RB) -> list[tuple[str, str, bool, str, str]]:
    """(id, description, mandatory, check, signal) rows for the frontmatter.

    Only two are mandatory, and both are always evaluable from the incident the
    executor was handed: the incident must still be open, and every step target must
    fall inside the declared scope. The observability-backed ones are advisory —
    Prometheus/Loki are frequently unreachable off-cluster, and a runbook that
    refuses to run because a *check* could not be performed would fail closed in the
    wrong direction for a demo. Matching still rewards a satisfied signal, and the
    dry run still reports the unknown as a warning.
    """
    rows: list[tuple[str, str, bool, str, str]] = [
        (
            "incident_active",
            "The incident is still open and within the configured max age.",
            True,
            "incident_active",
            "",
        ),
        (
            "target_in_scope",
            "Every step targets a service/namespace this runbook declares.",
            True,
            "service_scope",
            "",
        ),
    ]
    alerts, _category, signals = facets(rb)
    if alerts:
        rows.append(
            (
                "alert_firing",
                f"{alerts[0]} is still firing (advisory — skipped when Prometheus is unreachable).",
                False,
                "alert_firing",
                "",
            )
        )
    rows += [
        (
            f"signal_{sig}",
            f"The {sig} signal is present on the incident (advisory).",
            False,
            "signal_present",
            sig,
        )
        for sig in signals
    ]
    return rows


def render(rb: RB) -> str:
    # ── frontmatter: machine-executable, consumed by the runbook executor ──
    # Body-only fields (what / manual_* / expect) are deliberately NOT emitted
    # here — RunbookStep would reject the unknown keys.
    #
    # No created_at / updated_at: this generator's output is a pure function of the
    # table above, and a timestamp would make every regeneration a diff.
    alerts, category, signals = facets(rb)
    allowed_services, allowed_namespaces = target_scope(rb)
    lines = ["---", f"title: {rb.title}", f"service: {rb.service}", f"severity: {rb.severity}"]
    lines += [
        "version: 1",
        "status: active",
        f"owner: {SEED_OWNER}",
        f"approved_by: {SEED_APPROVER}",
    ]
    lines.append("tags:")
    lines += [f"- {t}" for t in rb.tags]
    lines.append("applicability:")
    lines.append("  environments:")
    lines += [f"  - {e}" for e in SEED_ENVIRONMENTS]
    if category:
        lines.append(f"  failure_category: {category}")
    if alerts:
        lines.append("  alerts:")
        lines += [f"  - {a}" for a in alerts]
    if signals:
        lines.append("  required_signals:")
        lines += [f"  - {s}" for s in signals]
    lines.append("  allowed_services:")
    lines += [f"  - {s}" for s in allowed_services]
    lines.append("  allowed_namespaces:")
    lines += [f"  - {n}" for n in allowed_namespaces]
    lines.append("prerequisites:")
    for pid, desc, mandatory, check, signal in prerequisites(rb):
        lines += [
            f"- id: {pid}",
            f"  description: {desc}",
            f"  mandatory: {str(mandatory).lower()}",
            f"  check: {check}",
        ]
        if signal:
            lines.append(f"  signal: {signal}")
    lines.append("steps:")
    for st in rb.steps:
        lines += [
            f"- name: {st.name}",
            f"  action: {st.action}",
            f"  destructive: {str(st.destructive).lower()}",
            f"  idempotent: {str(st.idempotent).lower()}",
        ]
        if st.rollback_action:
            lines.append(f"  rollback_action: {st.rollback_action}")
        lines += [f"  target: {st.target}", f"  namespace: {NAMESPACE}"]
    lines.append("---")

    # ── body: what a human (or the RCA agent) actually reads ───────────────
    lines += [
        f"# {rb.title}",
        "",
        "| | |",
        "|---|---|",
        f"| **Alert** | `{rb.alert}` |"
        if rb.alert
        else "| **Alert** | _none — this fault raises no Prometheus alert (see §2)_ |",
        f"| **Service** | `{rb.service}` |",
        f"| **Severity** | `{rb.severity}` |",
        "",
        "## 1. Symptoms",
        "",
        rb.symptom,
        "",
        "## 2. Confirm it is this failure",
        "",
        "Several ecommerce alerts are raised by more than one scenario, so the",
        "alert name alone does not identify the fault. Run these first.",
        "",
    ]
    if rb.diagnose:
        for desc, cmd, expect in rb.diagnose:
            lines += [f"**{desc}**", "", "```powershell", cmd, "```", "", f"Expect: {expect}", ""]
    else:
        lines += [
            "```powershell",
            f"kubectl -n {NAMESPACE} get pods",
            "```",
            "",
        ]

    lines += ["## 3. Root cause", "", rb.cause, "", "## 4. Procedure", ""]
    for i, st in enumerate(rb.steps, 1):
        flag = "**destructive - needs approval**" if st.destructive else "read-only"
        lines += [f"### Step {i}. {st.name}", "", f"`{st.action}` &middot; {flag}", ""]
        if st.what:
            lines += [st.what, ""]
        if st.manual_k8s:
            lines += [
                "Manual equivalent (Kubernetes):",
                "",
                "```powershell",
                st.manual_k8s,
                "```",
                "",
            ]
        if st.manual_compose:
            lines += [
                "Manual equivalent (Docker Compose):",
                "",
                "```powershell",
                st.manual_compose,
                "```",
                "",
            ]
        if st.expect:
            lines += [f"Expect: {st.expect}", ""]
        if st.rollback_action:
            lines += [f"Rollback if it fails: `{st.rollback_action}`", ""]

    lines += ["## 5. Verify the fix", ""]
    if rb.verify:
        for desc, cmd, expect in rb.verify:
            lines += [f"**{desc}**", "", "```", cmd, "```", "", f"Expect: {expect}", ""]
    elif rb.alert:
        lines += [f"- `{rb.alert}` clears in Prometheus.", ""]
    else:
        lines += ["- The symptom described in §1 is no longer reproducible.", ""]

    if rb.if_stuck:
        lines += ["## 6. If that did not fix it", ""]
        lines += [f"- {x}" for x in rb.if_stuck]
        lines.append("")

    if rb.notes:
        lines += ["## Notes", ""]
        lines += [f"- {n}" for n in rb.notes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _check_table() -> None:
    """Fail the generator when the table contradicts itself.

    Two ways this table can lie, both of which used to be possible: a runbook whose
    body names one alert while its applicability declares another, and a fault key
    with no facets row (which would raise a bare KeyError deep in render()).
    """
    problems: list[str] = []
    for rb in RUNBOOKS:
        key = fault_key(rb)
        if rb.generic or not key:
            continue
        if key not in FAULT_FACETS:
            problems.append(f"{rb.slug}: fault key {key!r} has no FAULT_FACETS row")
            continue
        declared = FAULT_FACETS[key][0]
        if rb.alert != declared:
            problems.append(
                f"{rb.slug}: body says alert={rb.alert!r} but FAULT_FACETS[{key!r}] "
                f"says {declared!r} — the body and the matcher must name the same rule"
            )
    if problems:
        raise SystemExit("generate_runbooks: inconsistent table\n  " + "\n  ".join(problems))


def main() -> int:
    _check_table()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for rb in RUNBOOKS:
        (OUT_DIR / f"{rb.slug}.md").write_text(render(rb), encoding="utf-8")
        print(f"  wrote {rb.slug}.md")
    print(f"\n{len(RUNBOOKS)} runbook(s) generated into {OUT_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
