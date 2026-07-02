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


# Service → list of services it depends on. Curated against the OTel demo
# call graph so RA-002's dependencies field has realistic content.
_DEPENDENCIES_MAPPING: dict[str, list[str]] = {
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


def _maybe_reset_feature_flag(action: str, target: str) -> dict[str, Any] | None:
    """If a runbook step is a feature-flag reset (``action='reset_feature_flag'``,
    ``target='flag/<name>'``), perform the REAL flip through the feature-flags
    seam so the injected demo scenario actually clears — instead of a mock no-op.

    Returns ``None`` when the step isn't a flag reset (caller falls back to the
    normal mock). Degrades gracefully: if the seam is unreachable (no flagd /
    off-cluster) it reports ``seam_ok=False`` but the *step* still succeeds, so
    the executor demo completes either way."""
    if action != "reset_feature_flag" or not target.startswith("flag/"):
        return None
    flag = target.split("/", 1)[1].strip()
    if not flag:
        return None
    seam_ok, detail = False, "feature-flags seam unavailable — simulated"
    try:
        from aiops.tools import get_registry

        res = get_registry().call("feature_flags.set_variant", flag=flag, variant="off")
        seam_ok = bool(res.ok)
        detail = res.data if res.ok else (res.error or detail)
    except Exception as exc:  # registry/seam not wired in this context
        detail = f"{type(exc).__name__}: {exc}"
    return {
        "feature_flag_reset": True,
        "flag": flag,
        "variant": "off",
        "seam_ok": seam_ok,
        "detail": detail,
    }


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
        "exit_code": 0,
        "stdout": (
            f"[dry-run] would {verb} {target or '<unspecified>'} in {namespace or 'default'}"
        ),
    }
    # A real feature-flag reset is performed ONLY here, on the REQUIRED-HITL
    # ``execute`` capability — so the live mutation physically cannot fire
    # without passing the human-approval gate (CLAUDE.md principle #3). The
    # ``apply`` (autonomous) path never mutates. Forward execute resets the flag
    # to off; a rollback re-injects it (back to the variant it was at).
    if mode != "rollback":
        ff = _maybe_reset_feature_flag(action, target)
        if ff is not None:
            data |= ff
            return ToolResult(
                ok=True,
                data=data,
                metadata={"provider": "feature-flags-seam" if ff["seam_ok"] else "mock"},
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
) -> ToolResult:
    """Preview what a step *would* do, without performing it.

    NONE-level (autonomous) by design: a dry-run makes no changes, so it never
    needs a human. The Runbook Executor (RA-004) calls this for every step
    before touching anything. Real Phase-2 swap target: ``ansible --check`` /
    ``kubectl --dry-run=server`` / an Argo workflow lint.
    """
    return ToolResult(
        ok=True,
        data={
            "step": step,
            "action": action or "<unspecified>",
            "target": target,
            "namespace": namespace or "default",
            "dry_run": True,
            "changes": [],
            "preview": f"[dry-run] would run {action or '<step>'} on {target or '<target>'}",
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
            "applied": True,
            "exit_code": 0,
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
