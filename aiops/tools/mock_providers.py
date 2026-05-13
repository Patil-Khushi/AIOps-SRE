"""Mock providers for the day-one capabilities.

These let Phase-0 smoke tests (and Phase-1 agent scaffolding) run without any
real backend. Each capability gets a real provider implementation in Phase 1+;
when that lands, agents do not change — only the registry's active provider does.

The ``itsm.cmdb.lookup`` mock is gated on ``AIOPS_USE_MOCK_ITSM`` so a developer
with a configured ServiceNow PDI gets the real CMDB lookup, while CI / tests
(which don't have PDI creds) keep getting the static table. The mock for
``itsm.incident.create`` is unconditional because the smoke test exercises it.
"""

from __future__ import annotations

import os

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
# ─────────────────────────────────────────────────────────────────────────────

# Service → owning team + runbook URL. Lowercase keys; agent code lowercases
# the alert's service before lookup. Covers the OTel demo's services and a
# few generic aliases ("payment-api" matches the canonical example input).
_CMDB_MAPPING: dict[str, dict[str, str | None]] = {
    "payment": {"team": "Payments Team", "runbook": "https://runbooks.example.com/payment-cpu"},
    "payment-api": {"team": "Payments Team", "runbook": "https://runbooks.example.com/payment-cpu"},
    "payment-service": {
        "team": "Payments Team",
        "runbook": "https://runbooks.example.com/payment-cpu",
    },
    "cart": {"team": "Order Experience", "runbook": "https://runbooks.example.com/cart"},
    "checkout": {"team": "Order Experience", "runbook": "https://runbooks.example.com/checkout"},
    "product-catalog": {"team": "Catalog Team", "runbook": "https://runbooks.example.com/catalog"},
    "product-reviews": {"team": "Catalog Team", "runbook": None},
    "recommendation": {
        "team": "Personalization Team",
        "runbook": "https://runbooks.example.com/recommendation",
    },
    "frontend": {"team": "Web Experience", "runbook": "https://runbooks.example.com/frontend"},
    "frontend-proxy": {"team": "Web Experience", "runbook": None},
    "shipping": {"team": "Fulfillment Team", "runbook": "https://runbooks.example.com/shipping"},
    "ad": {"team": "Ads Team", "runbook": None},
    "quote": {"team": "Pricing Team", "runbook": None},
    "currency": {"team": "Pricing Team", "runbook": None},
    "fraud-detection": {"team": "Trust and Safety", "runbook": None},
    "email": {"team": "Communications", "runbook": None},
    "accounting": {"team": "Finance Systems", "runbook": None},
    "image-provider": {"team": "Assets Team", "runbook": None},
}

_CMDB_DEFAULT: dict[str, str | None] = {"team": "Platform On-Call", "runbook": None}


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
        key = service.lower().strip()
        info = _CMDB_MAPPING.get(key, _CMDB_DEFAULT)
        return ToolResult(
            ok=True,
            data={"service": service, "team": info["team"], "runbook": info["runbook"]},
            metadata={"provider": "mock", "matched": key in _CMDB_MAPPING},
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
