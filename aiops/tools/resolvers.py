"""DB-backed provider for capability ``incident.resolvers.lookup``.

Institutional memory for the War-Room Assembler half of RA-005+006: when a war
room is marked *resolved*, the SMEs who joined (or the on-call as a fallback)
are recorded in ``incident_resolvers`` (see
``aiops.state.repository.save_incident_resolver``). On the next Sev-1/Sev-2
incident, the Notification Assembler asks this capability "who has resolved this
class of problem before?" and re-invites them alongside the on-call and the
dependency owners.

Vendor-neutral seam (CLAUDE.md #1): the agent calls
``get_registry().call("incident.resolvers.lookup", ...)`` and never imports
``aiops.state`` directly — same pattern as ``oncall.schedule.lookup``.

Registration happens at import time via ``@tool``. Hosts that want it active
(the demo server) simply ``import aiops.tools.resolvers`` at startup. In
contexts where it isn't imported (e.g. the eval harness), the agent's call
raises ``KeyError`` and the agent degrades to "no past resolvers" — never an
error.
"""

from __future__ import annotations

import logging

from aiops.state import repository as state_repo

from .registry import ToolResult, tool

logger = logging.getLogger(__name__)


@tool(
    name="db.incident.resolvers.lookup",
    capability="incident.resolvers.lookup",
    provider="db",
    description="Look up engineers who resolved past incidents on a service (optionally a failure sub-domain), newest first.",
)
def db_incident_resolvers_lookup(
    service: str,
    category: str | None = None,
    limit: int = 3,
) -> ToolResult:
    """Return the most recent distinct resolvers for ``service``.

    When ``category`` (failure sub-domain, e.g. ``"Payment Gateway"``) is given,
    scope to resolvers of that same sub-domain; otherwise fall back to
    service-wide. Best-effort: a DB blip returns ``ok=False`` rather than
    raising, so war-room assembly is never broken by this lookup.
    """
    try:
        resolvers = state_repo.list_incident_resolvers(
            affected_service=service,
            category=category,
            limit=max(1, int(limit)),
        )
    except Exception as exc:  # boundary: never let a DB blip break assembly
        logger.exception("incident.resolvers.lookup: DB lookup failed for service=%r", service)
        return ToolResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            metadata={"provider": "db", "service": service},
        )

    return ToolResult(
        ok=True,
        data={"service": service, "category": category, "resolvers": resolvers},
        metadata={"provider": "db", "count": len(resolvers)},
    )


__all__ = ["db_incident_resolvers_lookup"]
