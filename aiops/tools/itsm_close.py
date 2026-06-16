"""HITL-gated ServiceNow ticket closure — the verify → approve → close loop.

The resolution verifier only *proposes* closure; it never closes a ticket on
its own. This module is the platform-side executor, gated by the REQUIRED-HITL
capability ``itsm.ticket.close`` (registered in ``aiops/policy/gate.py`` and
mirrored in ``policies/hitl.rego``). Because the capability is REQUIRED,
``ToolRegistry.call`` posts an approve/deny card and blocks until a human
resolves it; only then does the tool body set the incident to Resolved via the
existing ``itsm.incident.update`` capability. Same machinery as the RCA
fix-step and KB-publish gates — reused, not reinvented.
"""

from __future__ import annotations

import os
from typing import Any

from aiops.tools.registry import ToolResult, get_registry, tool

_CAPABILITY = "itsm.ticket.close"
# ServiceNow incident states (override per instance if needed).
_RESOLVED_STATE = "6"  # Resolved
_CLOSED_STATE = "7"  # Closed
# Resolution code MUST be a valid choice in your instance, or ServiceNow's
# Data Policy blanks it and rejects the resolve ("Resolution code mandatory").
# Modern PDIs dropped "Solved (Permanently)"; set AIOPS_SERVICENOW_CLOSE_CODE to
# a value from your incident form's Resolution code dropdown.
_DEFAULT_CLOSE_CODE = "Solution provided"


@tool(
    name="seam.itsm.ticket.close",
    capability=_CAPABILITY,
    provider="seam",
    description="Resolve then Close a ServiceNow incident after HITL approval; proof lands in Resolution notes.",
)
def close_ticket(
    sys_id: str = "",
    close_code: str = "",
    close_notes: str = "",
    state: str = _RESOLVED_STATE,
    **_: Any,
) -> ToolResult:
    """Resolve, then Close an incident in two steps. Only reached after the
    REQUIRED-level ``itsm.ticket.close`` gate approves.

    Step 1 sets the incident to Resolved (state 6) with the resolution code and
    the full verification proof in ``close_notes`` — ServiceNow surfaces these
    in the incident's *Resolution Information* section. Step 2 advances it to
    Closed (state 7). We do two writes rather than a direct 6→7 because many
    instances enforce a Data Policy that an incident must pass through Resolved
    before it can be Closed. Both writes delegate to the OPTIONAL-level
    ``itsm.incident.update`` seam; the close step re-sends the resolution fields
    so they persist if the transition clears them."""
    if not sys_id:
        return ToolResult(ok=False, error="close_ticket requires a sys_id")
    # Env overrides win so the operator can match their instance's valid
    # Resolution code / Resolved / Closed states without touching code.
    resolved_state = state or os.environ.get("AIOPS_SERVICENOW_RESOLVED_STATE") or _RESOLVED_STATE
    closed_state = os.environ.get("AIOPS_SERVICENOW_CLOSED_STATE") or _CLOSED_STATE
    code = os.environ.get("AIOPS_SERVICENOW_CLOSE_CODE") or close_code or _DEFAULT_CLOSE_CODE
    notes = close_notes or "Resolved after automated verification (HITL-approved)."
    resolution_fields = {"state": resolved_state, "close_code": code, "close_notes": notes}

    reg = get_registry()
    # Step 1 — Resolve, carrying the resolution code + proof into Resolution notes.
    resolved = reg.call("itsm.incident.update", sys_id=sys_id, fields=resolution_fields)
    if not resolved.ok:
        return resolved
    # Step 2 — Close. Re-send the resolution fields so a transition that blanks
    # them still leaves the proof in Resolution Information.
    return reg.call(
        "itsm.incident.update",
        sys_id=sys_id,
        fields={**resolution_fields, "state": closed_state},
    )


def request_ticket_close(
    *,
    incident_id: str,
    sys_id: str,
    close_code: str = "",
    close_notes: str = "",
    hitl_context: dict[str, Any],
) -> dict[str, Any]:
    """Request approval to close a ticket; return a JSON-able outcome.

    Blocks inside the registry call until the human approves / denies / it
    expires (the platform gate owns the wait), then maps to
    ``closed`` / ``denied`` / ``expired`` / ``blocked`` / ``error``."""
    result = get_registry().call(
        _CAPABILITY,
        hitl_context=hitl_context,
        sys_id=sys_id,
        close_code=close_code,
        close_notes=close_notes,
    )

    approval_id = hitl_context.get("pending_approval_id")
    approver: str | None = None
    req = None
    if approval_id:
        try:
            from aiops.policy import get_approval_registry

            req = get_approval_registry().get(approval_id)
            approver = req.approver
        except Exception:
            req = None

    if result.ok:
        return {
            "status": "closed",
            "incident_id": incident_id,
            "approval_id": approval_id,
            "approver": approver,
        }

    status = "error"
    if (result.metadata or {}).get("blocked_by") == "hitl_gate":
        status = "blocked"
        if req is not None:
            try:
                from aiops.policy import ApprovalStatus

                if req.status is ApprovalStatus.DENIED:
                    status = "denied"
                elif req.status is ApprovalStatus.EXPIRED:
                    status = "expired"
            except Exception:
                pass
    return {
        "status": status,
        "incident_id": incident_id,
        "approval_id": approval_id,
        "approver": approver,
        "error": result.error,
    }
