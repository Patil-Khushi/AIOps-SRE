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
def mock_create_incident(short_description: str, urgency: int = 3) -> ToolResult:
    return ToolResult(
        ok=True,
        data={
            "id": "INC0000001",
            "short_description": short_description,
            "urgency": urgency,
            "state": "new",
        },
        metadata={"provider": "mock"},
    )


@tool(
    name="mock.notify.send",
    capability="notify.send",
    provider="mock",
    description="Pretend to send a chat notification.",
)
def mock_notify(channel: str, message: str) -> ToolResult:
    return ToolResult(ok=True, data={"channel": channel, "message": message[:200]})


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
