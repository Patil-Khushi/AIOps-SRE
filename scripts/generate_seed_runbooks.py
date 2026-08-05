"""Generate the Knowledge Synthesizer's seed runbooks from ecommerce scenarios.

Replaces the five seeds written for OTel Demo services (ad, cart, payment,
product-catalog, recommendation). Those services no longer exist, so the RAG
corpus was seeding the synthesizer with knowledge about a system that is gone —
worse than having no seeds, because retrieval returns confident, irrelevant
matches.

These are richer than the executor runbooks in agents/runbook_executor/runbooks/:
they carry Diagnosis / Verification / Rollback / Known-wrong-fixes sections and
are retrieval material, not an execution plan.

The "Known wrong fixes" section is the highest-value part. Each entry is a
plausible action an operator or an LLM would reach for that does NOT fix the
problem — restarting a pod whose fault lives in the pod spec, scaling a service
that is failing 100% of requests regardless of capacity, and so on.

    uv run python scripts/generate_seed_runbooks.py

Idempotent — output is a pure function of the table below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "agents" / "knowledge_synthesizer" / "seed_runbooks"
LAST_UPDATED = "2026-08-03"


@dataclass
class Seed:
    rid: str
    title: str
    service: str
    severity: str
    tags: list[str]
    scenario: str
    failure_key: str
    alert: str
    blast_radius: str
    symptoms: list[str]
    diagnosis: list[str]
    resolution: list[str]
    verification: list[str]
    rollback: list[str]
    wrong_fixes: list[str]


SEEDS: list[Seed] = [
    Seed(
        rid="rb-user-service-mysql-down",
        title="user-service — MySQL unavailable, all logins failing",
        service="user-service",
        severity="Sev-1",
        tags=["database", "mysql", "dependency", "login", "5xx"],
        scenario="user_service_mysql_down",
        failure_key="user_service.mysql_down",
        alert="EcommerceMySQLDown",
        blast_radius="LOW — scaling one StatefulSet back up; the PVC is retained so no data is lost.",
        symptoms=[
            "`EcommerceMySQLDown` firing; `mysql_connection_status == 0`.",
            "POST /login and /register return HTTP 500.",
            "Logs: `database connection failed`, connection refused to `mysql:3306`.",
            "Order creation also fails with 401 — order-service validates every order "
            "against user-service /profile.",
        ],
        diagnosis=[
            "Check `kubectl -n ecommerce get statefulset mysql` — replicas at 0 means the "
            "datastore was scaled down, not crashed.",
            "user-service pods are Running, not CrashLoopBackOff. That distinguishes this "
            "from the crashloop scenario, where MYSQL_HOST itself is wrong.",
            "/health returns HTTP 200 with `status: degraded` — deliberately, so a database "
            "outage does not restart the app pod.",
        ],
        resolution=[
            "**[clear_fault · low]** Scale the MySQL StatefulSet back to 1 and wait for the rollout.",
            "**[healthcheck · none]** Confirm `mysql_connection_status == 1` and a login succeeds.",
        ],
        verification=[
            "`mysql_connection_status` returns to 1 within ~30s.",
            "`EcommerceMySQLDown` clears.",
            "A register → login → order round trip succeeds.",
        ],
        rollback=["Scale the MySQL StatefulSet back to 0 to reproduce the fault."],
        wrong_fixes=[
            "Restart user-service — the app is healthy; its dependency is missing. A restart "
            "changes nothing and adds downtime.",
            "Scale up user-service replicas — every replica fails identically. Capacity is "
            "not the constraint.",
            "Delete the MySQL PVC to 'start clean' — this destroys all user accounts and "
            "does not address the scale-to-zero.",
        ],
    ),
    Seed(
        rid="rb-user-service-crashloop",
        title="user-service — CrashLoopBackOff from bad database host",
        service="user-service",
        severity="Sev-1",
        tags=["crashloop", "startup", "config", "database"],
        scenario="user_service_crashloop",
        failure_key="user_service.crashloop",
        alert="EcommerceServiceDown",
        blast_radius="LOW — correcting one environment variable; rolls forward automatically.",
        symptoms=[
            "Pod cycling CrashLoopBackOff with a climbing restartCount.",
            '`up{namespace="ecommerce"} == 0` for user-service; scrapes fail.',
            "Container logs end before uvicorn binds — no HTTP access lines at all.",
        ],
        diagnosis=[
            "`kubectl -n ecommerce describe pod` shows repeated restarts, not OOMKilled.",
            "`MYSQL_HOST` on the Deployment points at a host that does not resolve.",
            "mysql_client.py reads it via `os.environ[...]` at IMPORT time, so the process "
            "dies before serving — this is a startup failure, not a runtime one.",
        ],
        resolution=[
            "**[clear_fault · low]** Restore `MYSQL_HOST` to `mysql` on the Deployment.",
            "**[healthcheck · none]** Confirm the pod reaches Ready and scrapes resume.",
        ],
        verification=[
            "Pod Running and Ready; restartCount stops climbing.",
            "`up` returns to 1 and `EcommerceServiceDown` clears.",
        ],
        rollback=["Set `MYSQL_HOST` back to a non-resolving value to reproduce."],
        wrong_fixes=[
            "Restart the deployment — THE most common wrong move here. The bad value lives "
            "in the pod spec, so every new pod inherits it and crashloops identically.",
            "Scale MySQL up — MySQL is already healthy; the app is pointed at the wrong host.",
            "Increase the liveness probe's failureThreshold — this hides the crashloop "
            "without fixing it, and the service still never serves traffic.",
        ],
    ),
    Seed(
        rid="rb-order-service-memory-leak",
        title="order-service — memory leak leading to OOMKilled",
        service="order-service",
        severity="Sev-1",
        tags=["memory", "oom", "leak", "resource", "restart"],
        scenario="order_service_memory_leak",
        failure_key="order_service.memory_leak_oom",
        alert="EcommerceServiceDown",
        blast_radius="LOW — clearing one env toggle; the pod rolls automatically.",
        symptoms=[
            "Container RSS climbing steadily with order volume.",
            "Pod terminated with reason `OOMKilled`; restartCount incrementing.",
            "Brief scrape gaps and 5xx bursts during each restart.",
        ],
        diagnosis=[
            "`kubectl -n ecommerce describe pod` shows lastState.terminated.reason=OOMKilled.",
            "Memory growth correlates with order throughput, not with uptime — that points "
            "at a per-request leak rather than a slow accumulation.",
            "`INJECT_MEMORY_LEAK=true` is set; faults.py appends a 5 MB chunk per order to a "
            "module-global list that is never freed.",
        ],
        resolution=[
            "**[clear_fault · low]** Set `INJECT_MEMORY_LEAK=false` and restore the 256Mi limit.",
            "**[healthcheck · none]** Confirm RSS is flat under sustained order traffic.",
        ],
        verification=[
            "RSS stays flat while orders continue.",
            "restartCount stops incrementing; `up` stays at 1.",
        ],
        rollback=["Re-enable `INJECT_MEMORY_LEAK` to reproduce."],
        wrong_fixes=[
            "Raise the memory limit — this delays the OOM instead of fixing it. The leak is "
            "unbounded, so a larger limit only means a longer interval between kills.",
            "Restart the pod on a schedule — frees the leaked memory but the leak resumes "
            "immediately. This is treating the symptom.",
            "Scale out to more replicas — every replica leaks at the same per-order rate.",
        ],
    ),
    Seed(
        rid="rb-order-service-payment-timeout",
        title="order-service — payment calls timing out at the gateway",
        service="order-service",
        severity="Sev-2",
        tags=["timeout", "dependency", "payment", "gateway"],
        scenario="order_service_payment_timeout",
        failure_key="order_service.payment_timeout",
        alert="EcommercePaymentTimeouts",
        blast_radius="LOW — clearing a delay toggle on the mock gateway.",
        symptoms=[
            "`payment_timeout_total` climbing; POST /orders returns HTTP 504.",
            "Orders persisted then marked FAILED; the row is retained deliberately.",
            "Traces show the order → payment span exceeding the client timeout.",
        ],
        diagnosis=[
            "`INJECT_DELAY_SECONDS` on mock-payment-gateway exceeds order-service's "
            "`PAYMENT_TIMEOUT_SECONDS` (default 5s).",
            "The fault is on the GATEWAY, two hops downstream. The alert names the victim, "
            "not the cause — a recurring trap in this topology.",
            "payment-service itself is healthy; it is blocked waiting on its upstream.",
        ],
        resolution=[
            "**[clear_fault · low]** Reset `INJECT_DELAY_SECONDS` to 0 on mock-payment-gateway.",
            "**[healthcheck · none]** Confirm a full order completes and reaches PAID.",
        ],
        verification=[
            "`payment_timeout_total` stops climbing; `EcommercePaymentTimeouts` clears.",
            "POST /orders returns 201 with status PAID.",
        ],
        rollback=["Set `INJECT_DELAY_SECONDS` back above the client timeout."],
        wrong_fixes=[
            "Raise `PAYMENT_TIMEOUT_SECONDS` on order-service — this masks a broken "
            "dependency by making customers wait longer for the same failure.",
            "Restart order-service or payment-service — neither holds the fault.",
            "Scale up payment-service — it is idle, blocked on its upstream, not saturated.",
        ],
    ),
    Seed(
        rid="rb-payment-service-redis-down",
        title="payment-service — Redis unavailable, charges rejected",
        service="payment-service",
        severity="Sev-1",
        tags=["redis", "datastore", "dependency", "payments", "5xx"],
        scenario="payment_service_redis_down",
        failure_key="payment_service.redis_down",
        alert="EcommerceRedisDown",
        blast_radius="LOW — scaling one StatefulSet back up; the PVC is retained.",
        symptoms=[
            "`redis_connection_status == 0`; POST /payments returns 500.",
            "Logs: `redis connection error`.",
            "Orders reach PENDING then FAILED with reason `payment_failed`.",
        ],
        diagnosis=[
            "`kubectl -n ecommerce get statefulset redis` shows replicas at 0.",
            "Redis here is the payment RECORD STORE, not a cache — there is no fallback "
            "path, so the charge is rejected rather than silently unrecorded.",
            "payment-service pods stay Running: socket timeouts are capped at 2s so a dead "
            "Redis fails fast instead of hanging the request.",
        ],
        resolution=[
            "**[clear_fault · low]** Scale the Redis StatefulSet back to 1.",
            "**[healthcheck · none]** Confirm `redis_connection_status == 1` and a payment succeeds.",
        ],
        verification=[
            "`redis_connection_status` returns to 1; `EcommerceRedisDown` clears.",
            "A full order reaches PAID and a `payment:*` key appears in Redis.",
        ],
        rollback=["Scale Redis back to 0 to reproduce."],
        wrong_fixes=[
            "Treat this as a cache miss and 'degrade gracefully' — Redis is the system of "
            "record for payments here. Skipping it would take money without recording it.",
            "Restart payment-service — the app is healthy; its datastore is gone.",
            "Delete the Redis PVC — destroys every historical payment record.",
        ],
    ),
]


def render(s: Seed) -> str:
    lines = [
        "---",
        f"id: {s.rid}",
        f"title: {s.title}",
        f"service: {s.service}",
        "version: 1",
        f"tags: [{', '.join(s.tags)}]",
        f"severity: {s.severity}",
        "source: seed",
        "source_incident: null",
        "status: published",
        "related_kb: null",
        f"last_updated: {LAST_UPDATED}",
        "---",
        "",
        "## Symptoms",
    ]
    lines += [f"- {x}" for x in s.symptoms]
    lines += [
        "",
        "## Affected service & blast radius",
        f"`{s.service}`. Blast radius of the fix: **{s.blast_radius}**",
        "",
        "## Diagnosis",
    ]
    lines += [f"{i}. {x}" for i, x in enumerate(s.diagnosis, 1)]
    lines += ["", "## Resolution steps"]
    lines += [f"{i}. {x}" for i, x in enumerate(s.resolution, 1)]
    lines += ["", "## Verification"]
    lines += [f"- {x}" for x in s.verification]
    lines += ["", "## Rollback"]
    lines += [f"{i}. {x}" for i, x in enumerate(s.rollback, 1)]
    lines += ["", "## Known wrong fixes (do NOT do these)"]
    lines += [f"- {x}" for x in s.wrong_fixes]
    lines += [
        "",
        "## References",
        f"- Scenario: `{s.scenario}`",
        f"- Failure key: `{s.failure_key}`",
        f"- Alert rule: `{s.alert}`",
        f"- Truth file: `demo/ecommerce/truth_files/{s.scenario}.json`",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for s in SEEDS:
        (OUT_DIR / f"{s.rid}.md").write_text(render(s), encoding="utf-8")
        print(f"  wrote {s.rid}.md")
    print(f"\n{len(SEEDS)} seed runbook(s) generated into {OUT_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
