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
# ServiceNow incident state 6 = Resolved (override per instance if needed).
_RESOLVED_STATE = "6"
# Resolution code MUST be a valid choice in your instance, or ServiceNow's
# Data Policy blanks it and rejects the resolve ("Resolution code mandatory").
# Modern PDIs dropped "Solved (Permanently)"; set AIOPS_SERVICENOW_CLOSE_CODE to
# a value from your incident form's Resolution code dropdown.
_DEFAULT_CLOSE_CODE = "Solution provided"


@tool(
    name="seam.itsm.ticket.close",
    capability=_CAPABILITY,
    provider="seam",
    description="Resolve a ServiceNow incident (state→Resolved) after HITL approval.",
)
def close_ticket(
    sys_id: str = "",
    close_code: str = "",
    close_notes: str = "",
    state: str = _RESOLVED_STATE,
    **_: Any,
) -> ToolResult:
    """Set an incident to Resolved. Only reached after the HITL gate approves —
    REQUIRED-level ``itsm.ticket.close`` blocks upstream until a human approves.
    Delegates the write to the OPTIONAL-level ``itsm.incident.update`` seam."""
    if not sys_id:
        return ToolResult(ok=False, error="close_ticket requires a sys_id")
    # Env overrides win so the operator can match their instance's valid
    # Resolution code / Resolved state without touching code.
    resolved_state = state or os.environ.get("AIOPS_SERVICENOW_RESOLVED_STATE") or _RESOLVED_STATE
    code = os.environ.get("AIOPS_SERVICENOW_CLOSE_CODE") or close_code or _DEFAULT_CLOSE_CODE
    fields = {
        "state": resolved_state,
        "close_code": code,
        "close_notes": close_notes or "Resolved after automated verification (HITL-approved).",
    }
    return get_registry().call("itsm.incident.update", sys_id=sys_id, fields=fields)


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
