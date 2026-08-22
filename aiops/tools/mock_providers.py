"""Mock providers for the day-one capabilities.

These let Phase-0 smoke tests (and Phase-1 agent scaffolding) run without any
real backend. Each capability gets a real provider implementation in Phase 1+;
when that lands, agents do not change — only the registry's active provider does.

The ``itsm.cmdb.lookup`` mock is gated on ``AIOPS_USE_MOCK_ITSM`` so a developer
with a configured ServiceNow PDI gets the real CMDB lookup, while CI / tests
(which don't have PDI creds) keep getting the static table. The mock for
``itsm.incident.create`` is unconditional because the smoke test exercises it.

The CMDB demo table itself lives in ``aiops.tools.itsm._demo_cmdb`` so the
real ServiceNow provider can fall back to it when a stock PDI has no
``cmdb_ci_service`` row for a demo service (see DEMO-1 / #53).
"""

from __future__ import annotations

import os
from typing import Any

from aiops.tools.itsm import _demo_cmdb

from .registry import ToolResult, tool


def _use_mock_itsm() -> bool:
    return os.environ.get("AIOPS_USE_MOCK_ITSM", "true").strip().lower() in {"1", "true", "yes"}


@tool(
    name="mock.itsm.incident.create",
    capability="itsm.incident.create",
    provider="mock",
    description="Pretend to create an ITSM incident; returns a fake ticket id.",
)
def mock_create_incident(
    short_description: str,
    urgency: int = 3,
    description: str | None = None,
    assignment_group: str | None = None,
    category: str | None = None,
) -> ToolResult:
    """Mirror the real ServiceNow ``create_incident`` signature.

    The extra kwargs are accepted (not just filtered out by the registry) so
    tests can assert that RA-003 actually forwarded them — silently dropping
    them would let a regression slide through CI.
    """
    return ToolResult(
        ok=True,
        data={
            "id": "INC0000001",
            "short_description": short_description,
            "urgency": urgency,
            "description": description,
            "assignment_group": assignment_group,
            "category": category,
            "state": "new",
        },
        metadata={"provider": "mock"},
    )


# In-memory resolved-incident store for the mock ITSM provider, so the SNOW
# watcher's full flow can be exercised without a real ServiceNow instance
# (tests inject their own itsm_call; this is for manual/demo use). Append to it
# via ``add_mock_resolved_incident``.
_MOCK_RESOLVED_INCIDENTS: list[dict[str, Any]] = []


def add_mock_resolved_incident(incident: dict[str, Any]) -> None:
    """Register a resolved incident the mock ``itsm.incident.query`` will return.
    Demo/manual-test helper for running the watcher without a real PDI."""
    _MOCK_RESOLVED_INCIDENTS.append(dict(incident))


def clear_mock_resolved_incidents() -> None:
    _MOCK_RESOLVED_INCIDENTS.clear()


# In-memory incident store for mock get/update/close, so the verifier + ticket
# close flow is exercisable without a real ServiceNow. Keyed by sys_id, with a
# number→sys_id index. ``add_mock_incident`` seeds one; ``get_mock_incident``
# lets tests assert on work_notes / state after an update.
_MOCK_INCIDENTS: dict[str, dict[str, Any]] = {}  # sys_id -> record
_MOCK_NUMBER_INDEX: dict[str, str] = {}  # number -> sys_id


def add_mock_incident(number: str, sys_id: str, **fields: Any) -> None:
    rec = {"number": number, "sys_id": sys_id, "state": "2"}
    rec.update(fields)
    _MOCK_INCIDENTS[sys_id] = rec
    _MOCK_NUMBER_INDEX[number] = sys_id


def get_mock_incident(
    *, number: str | None = None, sys_id: str | None = None
) -> dict[str, Any] | None:
    if sys_id is None and number is not None:
        sys_id = _MOCK_NUMBER_INDEX.get(number)
    return _MOCK_INCIDENTS.get(sys_id) if sys_id else None


def clear_mock_incidents() -> None:
    _MOCK_INCIDENTS.clear()
    _MOCK_NUMBER_INDEX.clear()


if _use_mock_itsm():

    @tool(
        name="mock.itsm.incident.query",
        capability="itsm.incident.query",
        provider="mock",
        description="Return mock resolved incidents (demo/testing without a real PDI).",
    )
    def mock_query_incidents(query: str = "", fields: str = "", limit: int = 100) -> ToolResult:
        """Return the in-memory resolved-incident list. The encoded ``query`` is
        ignored — idempotency + the watcher's checkpoint handle dedup."""
        return ToolResult(
            ok=True,
            data={"incidents": list(_MOCK_RESOLVED_INCIDENTS)[: max(0, limit)]},
            metadata={"provider": "mock"},
        )

    @tool(
        name="mock.itsm.incident.get",
        capability="itsm.incident.get",
        provider="mock",
        description="Fetch a mock incident by number (resolve number → sys_id).",
    )
    def mock_get_incident(number: str = "", fields: str = "") -> ToolResult:
        rec = get_mock_incident(number=number)
        if rec is None:
            return ToolResult(
                ok=False, error=f"incident {number} not found", metadata={"provider": "mock"}
            )
        return ToolResult(ok=True, data={"incident": dict(rec)}, metadata={"provider": "mock"})

    @tool(
        name="mock.itsm.incident.update",
        capability="itsm.incident.update",
        provider="mock",
        description="Update a mock incident (work_notes / state / close fields).",
    )
    def mock_update_incident(sys_id: str = "", fields: dict[str, Any] | None = None) -> ToolResult:
        rec = _MOCK_INCIDENTS.get(sys_id)
        if rec is None:
            # Tolerate updates to unseeded incidents by materializing a record,
            # so the verifier's work-note write succeeds in a bare demo.
            rec = {"sys_id": sys_id, "number": sys_id, "state": "2"}
            _MOCK_INCIDENTS[sys_id] = rec
        rec.update(fields or {})
        return ToolResult(
            ok=True,
            data={"sys_id": sys_id, "number": rec.get("number"), "state": rec.get("state")},
            metadata={"provider": "mock"},
        )


@tool(
    name="mock.notify.send",
    capability="notify.send",
    provider="mock",
    description="Pretend to send a chat notification.",
)
def mock_notify(
    channel: str,
    message: str,
    severity: str | None = None,
    ticket_id: str | None = None,
) -> ToolResult:
    """``severity`` / ``ticket_id`` are accepted (not just dropped by the
    registry's signature filter) so tests can assert RA-003 forwarded them and
    a real chat adapter can colour-code + deep-link the message — mirrors the
    forwarded-kwargs pattern on ``mock_create_incident``."""
    return ToolResult(
        ok=True,
        data={
            "channel": channel,
            "message": message[:200],
            "severity": severity,
            "ticket_id": ticket_id,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# CMDB + on-call mocks (needed by Alert Triage step 7 — Resolve ownership)
#
# Real Phase-1 providers will hit ServiceNow CMDB (itsm.cmdb.lookup) and
# PagerDuty (oncall.schedule.lookup). The agent code does not change when
# those land — only the registry's active provider for these capabilities.
# The CMDB demo table lives in aiops.tools.itsm._demo_cmdb so the real
# ServiceNow provider can fall back to the same data on no-match.
# ─────────────────────────────────────────────────────────────────────────────


if _use_mock_itsm():

    @tool(
        name="mock.itsm.cmdb.lookup",
        capability="itsm.cmdb.lookup",
        provider="mock",
        description="Map service name to owning team + recommended runbook URL.",
    )
    def mock_cmdb_lookup(service: str) -> ToolResult:
        """Return CMDB-style ownership info for a service.

        Falls back to ``Platform On-Call`` when the service is unknown so the
        agent always has someone to route to.
        """
        info = _demo_cmdb.lookup(service) or _demo_cmdb.CMDB_DEFAULT
        matched = _demo_cmdb.lookup(service) is not None
        return ToolResult(
            ok=True,
            data={"service": service, "team": info["team"], "runbook": info["runbook"]},
            metadata={"provider": "mock", "matched": matched},
        )


def _team_slug(team: str) -> str:
    """Turn ``"Payments Team"`` into ``"payments"`` for the synthetic email."""
    slug = team.lower()
    if slug.endswith(" team"):
        slug = slug[: -len(" team")]
    return slug.replace(" ", "-").replace("&", "and")


@tool(
    name="mock.oncall.schedule.lookup",
    capability="oncall.schedule.lookup",
    provider="mock",
    description="Return the engineer currently on-call for a team (synthetic email).",
)
def mock_oncall_lookup(team: str) -> ToolResult:
    """Return a synthetic on-call email for a team.

    Phase-1 swap target: PagerDuty's GET /oncalls. Until then, every team gets
    a deterministic ``oncall@<team-slug>.example.com`` so verdicts are
    reproducible in evals.
    """
    return ToolResult(
        ok=True,
        data={"team": team, "engineer_email": f"oncall@{_team_slug(team)}.example.com"},
        metadata={"provider": "mock"},
    )


# Service → list of services it depends on. Two call graphs live here.
#
# The `ecommerce-*` block is the CURRENT system under test (demo/ecommerce/) and
# is derived from the real wiring, not sketched: each edge below corresponds to
# an env var the caller actually reads — USER_SERVICE_URL / PAYMENT_SERVICE_URL
# in order-service, GATEWAY_URL + REDIS_HOST in payment-service, MYSQL_HOST in
# user-service (see demo/ecommerce/k8s/01-config.yaml and docker-compose.yml).
#
# The unprefixed block is the OpenTelemetry Demo (astronomy shop) call graph.
# Those workloads are scaled to zero, but the entries stay: the log_correlation
# golden evals in agents/log_correlation/evals/golden.json are keyed on these
# names and gate CI at --min-pass-rate 0.85. Deleting them would fail the build
# to no purpose. They are inert once nothing queries those service names.
#
# Both naming forms of the ecommerce services are listed because both are in
# live use: OTEL_SERVICE_NAME (and therefore the Prometheus / Loki / Jaeger
# `service_name` label) carries the `ecommerce-` prefix, while the truth files,
# scenario YAMLs and alert payloads in demo/ecommerce/ use the bare name.
# Lookup is an exact dict hit on a lowercased key — no normalisation — so an
# alias that is missing simply resolves to "no topology".
_DEPENDENCIES_MAPPING: dict[str, list[str]] = {
    # ── ecommerce SUT — telemetry naming (OTEL_SERVICE_NAME) ────────────────
    "ecommerce-frontend": ["ecommerce-user-service", "ecommerce-order-service"],
    "ecommerce-user-service": ["mysql"],
    "ecommerce-order-service": [
        "ecommerce-user-service",
        "ecommerce-payment-service",
        "postgres",
    ],
    "ecommerce-payment-service": ["ecommerce-mock-payment-gateway", "redis"],
    "ecommerce-mock-payment-gateway": [],
    # ── ecommerce SUT — bare naming (truth files, scenarios, alert payloads) ─
    # No bare "frontend" alias: that key belongs to the astronomy shop below and
    # the ecommerce SPA is only ever labelled `ecommerce-frontend`. Claiming the
    # shared key would silently hand astronomy-shop dependencies to whichever
    # caller asked first.
    "user-service": ["mysql"],
    "order-service": ["user-service", "payment-service", "postgres"],
    "payment-service": ["mock-payment-gateway", "redis"],
    "mock-payment-gateway": [],
    # Datastores are leaves: they are the bottom of this graph, and saying so
    # explicitly is not the same as failing to find them. `matched` in the
    # ToolResult metadata is what distinguishes "leaf" from "unknown service".
    "mysql": [],
    "postgres": [],
    "redis": [],
    # ── OpenTelemetry Demo (astronomy shop) — retained for the golden evals ──
    "frontend": [
        "cart",
        "checkout",
        "product-catalog",
        "recommendation",
        "currency",
        "ad",
        "shipping",
    ],
    "frontend-proxy": ["frontend"],
    "checkout": ["cart", "payment", "shipping", "email", "currency"],
    "cart": ["product-catalog"],
    "payment": ["currency", "fraud-detection"],
    "recommendation": ["product-catalog"],
    "shipping": ["quote"],
    "ad": [],
    "quote": [],
    "currency": [],
    "product-catalog": [],
    "product-reviews": [],
    "fraud-detection": [],
    "email": [],
    "accounting": [],
    "image-provider": [],
}


def _maybe_clear_fault(action: str, target: str) -> dict[str, Any] | None:
    """Perform the REAL recovery for a fault-clearing runbook step.

    Recognises ``action='clear_fault'`` with ``target='fault/<failure_key>'``
    and dispatches to the ``automation.fault.clear`` seam. Returns ``None`` when
    the step is not a fault clear, so the caller falls back to the normal mock.

    ``reset_feature_flag`` / ``flag/<name>`` is the pre-migration spelling from
    when faults were flagd flags; still accepted so a hand-written runbook that
    predates the migration keeps working rather than silently becoming a no-op.

    **This never reports success it did not achieve.** ``fault_cleared`` says
    whether the fault is actually gone, and the caller turns that straight into
    ``ToolResult.ok``. It previously returned ``feature_flag_reset: True``
    unconditionally with the real outcome buried in ``seam_ok``, which nothing
    read — so a human-approved destructive step reported "executed" while the
    fault was still firing. Tests that need this to pass off-cluster register a
    stub provider for the capability rather than relying on the tool to lie.
    """
    if action not in ("clear_fault", "reset_feature_flag"):
        return None
    if not (target.startswith("fault/") or target.startswith("flag/")):
        return None
    fault = target.split("/", 1)[1].strip()
    if not fault:
        return None

    from aiops.tools import get_registry

    try:
        res = get_registry().call("automation.fault.clear", fault=fault, target="off")
    except Exception as exc:  # boundary: a broken seam must not crash the executor
        return {
            "fault_cleared": False,
            "fault": fault,
            "seam_ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
        }

    missing = bool((res.metadata or {}).get("missing_provider"))
    return {
        "fault_cleared": bool(res.ok),
        "fault": fault,
        "seam_ok": bool(res.ok),
        "missing_provider": missing,
        "detail": res.data if res.ok else (res.error or "fault-clear seam reported failure"),
    }


# ── Deterministic prediction/observation helpers (RA-004 sim-vs-exec, #213) ──
# Shared by simulate (prediction) and execute/apply (observation) so a mock run
# matches by construction; divergence is exercised via test fault providers.
# All values are deterministic — no wall-clock — so the eval harness stays stable.

_RISKY_ACTION_MARKERS = ("restart", "scale", "rollback", "delete", "reset", "drain")


def _side_effects_for(action: str, target: str) -> list[str]:
    """The side effect(s) a step's action produces, as stable string tokens."""
    act = (action or "").strip()
    if not act:
        return []
    tgt = (target or "").strip()
    return [f"{act}:{tgt}"] if tgt else [act]


def _warnings_for(action: str) -> list[str]:
    a = (action or "").lower()
    if any(m in a for m in _RISKY_ACTION_MARKERS):
        return [f"{action} may briefly disrupt traffic"]
    return []


def _estimated_duration_ms(action: str) -> int:
    """A deterministic synthetic duration estimate (ms). Riskier actions are
    modelled as slower so the demo shows non-uniform predictions."""
    base = 500
    if any(m in (action or "").lower() for m in _RISKY_ACTION_MARKERS):
        base = 1500
    return base


@tool(
    name="mock.automation.runbook.execute",
    capability="automation.runbook.execute",
    provider="mock",
    description="Pretend to execute a runbook step. Used by the HITL-1 demo path.",
)
def mock_runbook_execute(
    runbook: str = "",
    target: str = "",
    namespace: str = "",
    dry_run: bool = True,
    step: str = "",
    action: str = "",
    mode: str = "execute",
    params: dict[str, Any] | None = None,
) -> ToolResult:
    """Execute (or roll back) a DESTRUCTIVE runbook step. REQUIRED-HITL.

    Used by the auto_healer_lite demo agent (HITL-1) and the Runbook Executor
    (RA-004). ``step``/``action``/``mode`` are optional so RA-004 can carry
    step identity and distinguish a forward ``execute`` from a ``rollback``
    call for the audit log; auto_healer_lite omits them and keeps working.

    Real Phase-2 swap target: an Ansible AWX / kubectl shell-out / Argo
    workflow.  For the POC the action only needs to *prove* that the gate
    physically blocked it without an approver — the side-effect itself is
    irrelevant.  Returns a deterministic dict so the demo can assert on it.
    """
    verb = "roll back" if mode == "rollback" else "restart"
    data: dict[str, Any] = {
        "runbook": runbook or "restart-deployment",
        "target": target,
        "namespace": namespace or "default",
        "dry_run": dry_run,
        "step": step,
        "action": action,
        "mode": mode,
        # Validated step parameters, echoed back so the audit trail records what was
        # actually passed. RA-004 validates these against the action registry's schema
        # before dispatch (agents/runbook_executor/actions.py) — a provider never has to
        # trust them, and one that does not accept them is unaffected (the registry
        # filters kwargs by signature).
        "params": dict(params or {}),
        "exit_code": 0,
        "stdout": (
            f"[dry-run] would {verb} {target or '<unspecified>'} in {namespace or 'default'}"
        ),
        # Observed outcome for RA-004's sim-vs-execution comparison (#213).
        "actual_side_effects": _side_effects_for(action, target),
        "duration_ms": _estimated_duration_ms(action),
    }
    # A real fault clear is performed ONLY here, on the REQUIRED-HITL
    # ``execute`` capability — so the live mutation physically cannot fire
    # without passing the human-approval gate (CLAUDE.md principle #3). The
    # ``apply`` (autonomous) path never mutates. Forward execute clears the
    # fault; a rollback would re-inject it, which this capability refuses.
    #
    # `ok` here answers "is the fault cleared?", never "did the mock run?".
    # This is the one action on this path with a real side effect, and it was
    # approved by a human — overstating it is the worst outcome available.
    # Everything else on this path stays a mock and keeps returning ok=True.
    if mode != "rollback":
        cleared = _maybe_clear_fault(action, target)
        if cleared is not None:
            data |= cleared
            if cleared["fault_cleared"]:
                provider = "ecommerce-fault-seam"
            elif cleared.get("missing_provider"):
                provider = "unregistered"
            else:
                provider = "seam-failed"
            return ToolResult(
                ok=cleared["fault_cleared"],
                data=data,
                error=None if cleared["fault_cleared"] else str(cleared["detail"]),
                metadata={"provider": provider, "fault_cleared": cleared["fault_cleared"]},
            )
    return ToolResult(ok=True, data=data, metadata={"provider": "mock"})


@tool(
    name="mock.automation.runbook.simulate",
    capability="automation.runbook.simulate",
    provider="mock",
    description="Dry-run preview of a runbook step. Read-only — never changes anything.",
)
def mock_runbook_simulate(
    step: str = "",
    action: str = "",
    target: str = "",
    namespace: str = "",
    params: dict[str, Any] | None = None,
) -> ToolResult:
    """Preview what a step *would* do, without performing it.

    NONE-level (autonomous) by design: a dry-run makes no changes, so it never
    needs a human. The Runbook Executor (RA-004) calls this for every step
    before touching anything. Real Phase-2 swap target: ``ansible --check`` /
    ``kubectl --dry-run=server`` / an Argo workflow lint.
    """
    preview = f"[dry-run] would run {action or '<step>'} on {target or '<target>'}"
    return ToolResult(
        ok=True,
        data={
            "step": step,
            "action": action or "<unspecified>",
            "target": target,
            "namespace": namespace or "default",
            "params": dict(params or {}),
            "dry_run": True,
            "changes": [],
            "preview": preview,
            # Full prediction for RA-004's simulation detail + comparison (#213).
            # Non-mutating: these are predictions only, no live system is touched.
            "predicted_actions": [action] if action else [],
            "warnings": _warnings_for(action),
            "estimated_duration_ms": _estimated_duration_ms(action),
            "predicted_side_effects": _side_effects_for(action, target),
            "summary": preview,
        },
        metadata={"provider": "mock"},
    )


@tool(
    name="mock.automation.runbook.apply",
    capability="automation.runbook.apply",
    provider="mock",
    description="Execute a NON-destructive runbook step autonomously (NONE-level).",
)
def mock_runbook_apply(
    step: str = "",
    action: str = "",
    target: str = "",
    namespace: str = "",
    params: dict[str, Any] | None = None,
) -> ToolResult:
    """Run a non-destructive step (drain, snapshot, health-check, …).

    NONE-level: non-destructive steps run without a human so the gate fires
    only on the destructive ones. Destructive steps go through the REQUIRED
    ``automation.runbook.execute`` capability instead.

    This path NEVER mutates the live system — a real feature-flag reset is a
    REQUIRED-HITL action and is performed only on the ``execute`` capability
    (see ``mock_runbook_execute``), so it cannot fire without a human gate.
    """
    return ToolResult(
        ok=True,
        data={
            "step": step,
            "action": action or "<unspecified>",
            "target": target,
            "namespace": namespace or "default",
            "params": dict(params or {}),
            "applied": True,
            "exit_code": 0,
            # Observed outcome for RA-004's sim-vs-execution comparison (#213).
            "actual_side_effects": _side_effects_for(action, target),
            "duration_ms": _estimated_duration_ms(action),
        },
        metadata={"provider": "mock"},
    )


@tool(
    name="mock.itsm.cmdb.dependencies",
    capability="itsm.cmdb.dependencies",
    provider="mock",
    description="Return the list of downstream services a given service depends on.",
)
def mock_cmdb_dependencies(service: str) -> ToolResult:
    """Return service dependencies from the CMDB.

    Phase-1 swap target: real CMDB relationship traversal (ServiceNow CI
    relationships, or a topology graph derived from OTel trace data).
    """
    key = service.lower().strip()
    deps = _DEPENDENCIES_MAPPING.get(key, [])
    return ToolResult(
        ok=True,
        data={"service": service, "dependencies": list(deps)},
        metadata={"provider": "mock", "matched": key in _DEPENDENCIES_MAPPING},
    )
