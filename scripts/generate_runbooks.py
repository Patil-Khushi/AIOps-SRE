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


def _health(service: str) -> Step:
    np, cp = PORTS.get(service, (0, 0))
    return Step(
        "verify-health",
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
        slug="user-service-high-cpu",
        title="user-service — CPU saturation",
        service="user-service",
        severity="sev2",
        tags=["cpu", "saturation", "latency", "throttling"],
        alert="EcommerceOrderLatencyHigh",
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
            _health("mock-payment-gateway"),
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
        alert="EcommerceServiceDown",
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
            _health("mock-payment-gateway"),
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
        alert="EcommerceOrderLatencyHigh",
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
        symptom="payment-service degraded with no identified injected fault.",
        cause="Unknown. Use when the specific fault runbooks do not match.",
        steps=[*_restart_pair("payment-service"), _health("payment-service")],
        notes=[
            "If the symptom returns after the restart, an injected fault is still set — "
            "check the scenario catalog before restarting again."
        ],
    ),
]


def render(rb: RB) -> str:
    # ── frontmatter: machine-executable, consumed by the runbook executor ──
    # Body-only fields (what / manual_* / expect) are deliberately NOT emitted
    # here — RunbookStep would reject the unknown keys.
    lines = ["---", f"title: {rb.title}", f"service: {rb.service}", f"severity: {rb.severity}"]
    lines.append("tags:")
    lines += [f"- {t}" for t in rb.tags]
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
        f"| **Alert** | `{rb.alert}` |",
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
    else:
        lines += [f"- `{rb.alert}` clears in Prometheus.", ""]

    if rb.if_stuck:
        lines += ["## 6. If that did not fix it", ""]
        lines += [f"- {x}" for x in rb.if_stuck]
        lines.append("")

    if rb.notes:
        lines += ["## Notes", ""]
        lines += [f"- {n}" for n in rb.notes]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for rb in RUNBOOKS:
        (OUT_DIR / f"{rb.slug}.md").write_text(render(rb), encoding="utf-8")
        print(f"  wrote {rb.slug}.md")
    print(f"\n{len(RUNBOOKS)} runbook(s) generated into {OUT_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
