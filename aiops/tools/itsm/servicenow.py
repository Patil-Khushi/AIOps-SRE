"""ServiceNow provider for the ``itsm.*`` capabilities (RA-003 Auto-Ticketing).

Three capabilities are registered when ``AIOPS_USE_MOCK_ITSM`` is ``false``:

- ``itsm.incident.create`` — POST ``/api/now/table/incident``
- ``itsm.incident.update`` — PATCH ``/api/now/table/incident/{sys_id}``
- ``itsm.cmdb.lookup``     — GET  ``/api/now/table/cmdb_ci_service`` by service name

The provider authenticates against a ServiceNow PDI via basic auth. Credentials
come from the environment (``AIOPS_SERVICENOW_*``) and are read lazily on every
call so a developer can switch tenants by editing ``.env`` and re-running —
no module reload required.

The agent calling these capabilities never sees ServiceNow directly: it asks
the registry for ``itsm.incident.create`` and gets a ServiceNow incident sys_id
or a Jira issue key back, depending on which provider is active. Swapping the
provider is a registry config change, not an agent rewrite (see CLAUDE.md §1).

Registration is gated symmetrically with the mock provider: ``AIOPS_USE_MOCK_ITSM=true``
(the CI / no-PDI default) registers only the mocks; ``=false`` registers only
the real ServiceNow tools. This avoids registry order-of-import races between
the two providers and keeps the test suite deterministic.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from aiops.tools.itsm import _demo_cmdb
from aiops.tools.registry import ToolResult, tool


def _use_mock_itsm() -> bool:
    """Mirror of ``aiops.tools.mock_providers._use_mock_itsm``.

    Duplicated rather than imported to keep the two providers independent —
    neither module should pull the other in just to check a flag.
    """
    return os.environ.get("AIOPS_USE_MOCK_ITSM", "true").strip().lower() in {"1", "true", "yes"}


def _register_if_real(**kwargs: Any) -> Any:
    """Apply ``@tool`` when ``AIOPS_USE_MOCK_ITSM`` is false; otherwise no-op.

    Keeps the three capability definitions side-by-side and readable while
    making registration symmetric with the mock provider's gate.
    """
    if _use_mock_itsm():
        return lambda fn: fn
    return tool(**kwargs)


def _config() -> tuple[str, httpx.BasicAuth, float, bool | str] | None:
    """Return (base_url, auth, timeout, verify) or ``None`` if creds are unset.

    Read lazily so ``.env`` edits take effect on the next call without
    re-importing the module.

    ``verify`` controls httpx's TLS verification:
    - default (unset / ``true``) — verify against the system CA bundle.
    - a file path — verify against that custom CA bundle (the right answer
      on a corp network: point this at the Zensar root CA PEM).
    - ``false`` — skip verification entirely. POC escape hatch when a
      TLS-intercepting proxy sits between the agent and the PDI; never use
      in production.
    """
    url = os.environ.get("AIOPS_SERVICENOW_INSTANCE_URL", "").rstrip("/")
    user = os.environ.get("AIOPS_SERVICENOW_USER", "")
    password = os.environ.get("AIOPS_SERVICENOW_PASSWORD", "")
    if not (url and user and password):
        return None
    timeout = float(os.environ.get("AIOPS_SERVICENOW_TIMEOUT", "15"))
    verify_raw = os.environ.get("AIOPS_SERVICENOW_VERIFY_TLS", "true").strip()
    verify: bool | str
    if verify_raw.lower() in {"0", "false", "no"}:
        verify = False
    elif verify_raw.lower() in {"1", "true", "yes", ""}:
        verify = True
    else:
        # Anything else is treated as a CA bundle path.
        verify = verify_raw
    return url, httpx.BasicAuth(username=user, password=password), timeout, verify


def _not_configured() -> ToolResult:
    return ToolResult(
        ok=False,
        error=(
            "ServiceNow not configured: set AIOPS_SERVICENOW_INSTANCE_URL, "
            "AIOPS_SERVICENOW_USER, AIOPS_SERVICENOW_PASSWORD in .env"
        ),
        metadata={"provider": "servicenow", "configured": False},
    )


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    json: dict[str, Any] | None = None,
) -> ToolResult:
    cfg = _config()
    if cfg is None:
        return _not_configured()
    base_url, auth, timeout, verify = cfg
    try:
        r = httpx.request(
            method,
            f"{base_url}{path}",
            params=params,
            json=json,
            auth=auth,
            timeout=timeout,
            verify=verify,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        return ToolResult(
            ok=False,
            error=f"HTTPError: {exc}",
            metadata={"provider": "servicenow"},
        )
    if r.status_code >= 400:
        # ServiceNow's table API returns its own JSON error shape; surface it
        # verbatim so the caller can distinguish 401 (creds) from 403 (ACL)
        # from 404 (no such record).
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:500]}
        return ToolResult(
            ok=False,
            error=f"status={r.status_code} body={body}",
            metadata={"provider": "servicenow", "status_code": r.status_code},
        )
    try:
        return ToolResult(
            ok=True,
            data=r.json(),
            metadata={"provider": "servicenow", "url": base_url},
        )
    except ValueError as exc:
        return ToolResult(
            ok=False,
            error=f"non-JSON response: {exc}",
            metadata={"provider": "servicenow"},
        )


# ─────────────────────────────────────────────────────────────────────────────
# itsm.incident.create
# ─────────────────────────────────────────────────────────────────────────────


@_register_if_real(
    name="snow.itsm.incident.create",
    capability="itsm.incident.create",
    provider="servicenow",
    description="Create a ServiceNow incident. Returns the new record's sys_id and number.",
)
def create_incident(
    short_description: str,
    urgency: int = 3,
    description: str | None = None,
    assignment_group: str | None = None,
    caller_id: str | None = None,
    category: str | None = None,
) -> ToolResult:
    """POST a new incident to ServiceNow.

    ``urgency`` follows ServiceNow's 1=High / 2=Medium / 3=Low convention.
    ``assignment_group`` accepts either a group sys_id or its display name —
    ServiceNow resolves either to the same record.
    """
    payload: dict[str, Any] = {
        "short_description": short_description,
        "urgency": str(urgency),
    }
    if description is not None:
        payload["description"] = description
    if assignment_group is not None:
        payload["assignment_group"] = assignment_group
    if caller_id is not None:
        payload["caller_id"] = caller_id
    if category is not None:
        payload["category"] = category
    res = _request("POST", "/api/now/table/incident", json=payload)
    if not res.ok:
        return res
    record = (res.data or {}).get("result", {})
    return ToolResult(
        ok=True,
        data={
            "sys_id": record.get("sys_id"),
            "number": record.get("number"),
            "state": record.get("state"),
            "short_description": record.get("short_description"),
            "urgency": record.get("urgency"),
        },
        metadata=res.metadata,
    )


# ─────────────────────────────────────────────────────────────────────────────
# itsm.incident.update
# ─────────────────────────────────────────────────────────────────────────────


@_register_if_real(
    name="snow.itsm.incident.update",
    capability="itsm.incident.update",
    provider="servicenow",
    description="PATCH fields on an existing ServiceNow incident by sys_id.",
)
def update_incident(sys_id: str, fields: dict[str, Any]) -> ToolResult:
    """PATCH an incident.

    ``fields`` is a dict forwarded as-is to ServiceNow — caller decides whether
    to set ``state``, ``work_notes``, ``close_code``, etc. Field names must
    match the ``incident`` table's column names. An explicit dict (rather than
    ``**kwargs``) is required because the registry filters call kwargs against
    the function signature, which collapses var-kwargs to a single parameter
    name and drops everything else.
    """
    if not sys_id:
        return ToolResult(
            ok=False,
            error="update_incident requires sys_id",
            metadata={"provider": "servicenow"},
        )
    if not fields:
        return ToolResult(
            ok=False,
            error="update_incident requires at least one field to update",
            metadata={"provider": "servicenow"},
        )
    res = _request("PATCH", f"/api/now/table/incident/{sys_id}", json=fields)
    if not res.ok:
        return res
    record = (res.data or {}).get("result", {})
    return ToolResult(
        ok=True,
        data={
            "sys_id": record.get("sys_id"),
            "number": record.get("number"),
            "state": record.get("state"),
        },
        metadata=res.metadata,
    )


# ─────────────────────────────────────────────────────────────────────────────
# itsm.cmdb.lookup
# ─────────────────────────────────────────────────────────────────────────────

# Custom field that may carry a runbook URL in a customer-tailored CMDB.
# Stock PDIs don't ship with this column, so the lookup falls back to ``None``
# when it's absent — the agent's "Platform On-Call" default kicks in.
_RUNBOOK_FIELD = os.environ.get("AIOPS_SERVICENOW_RUNBOOK_FIELD", "u_runbook_url")


@_register_if_real(
    name="snow.itsm.cmdb.lookup",
    capability="itsm.cmdb.lookup",
    provider="servicenow",
    description="Look up a service in cmdb_ci_service; return owning team + runbook URL.",
)
def cmdb_lookup(service: str) -> ToolResult:
    """Resolve a service name to its owning team + runbook URL.

    Queries ``cmdb_ci_service`` by ``name`` (case-insensitive, exact match
    first then ``LIKE``). When the real PDI returns no row — which is the
    common case for the OpenTelemetry Astronomy Shop services on a stock
    Personal Developer Instance — we fall back to the demo CMDB table in
    ``aiops.tools.itsm._demo_cmdb`` so ownership routing keeps working
    end-to-end (DEMO-1 / #53). The fallback is signalled via metadata
    (``fallback="demo_cmdb"``) so callers/agents can see what happened.
    """
    key = (service or "").strip()
    if not key:
        return ToolResult(
            ok=False,
            error="cmdb_lookup requires a non-empty service name",
            metadata={"provider": "servicenow"},
        )
    # ``sysparm_display_value=true`` resolves the support_group reference to its
    # display name in the same call — saves a round-trip and a second ACL check.
    res = _request(
        "GET",
        "/api/now/table/cmdb_ci_service",
        params={
            "sysparm_query": f"name={key}^ORnameLIKE{key}",
            "sysparm_display_value": "true",
            "sysparm_fields": f"name,support_group,sys_class_name,operational_status,{_RUNBOOK_FIELD}",
            "sysparm_limit": "1",
        },
    )
    if not res.ok:
        return res
    results = (res.data or {}).get("result", []) or []
    if not results:
        return _demo_cmdb_fallback(service)
    row = results[0]
    return ToolResult(
        ok=True,
        data={
            "service": _resolve(row.get("name")) or service,
            "team": _resolve(row.get("support_group")),
            "runbook": _resolve(row.get(_RUNBOOK_FIELD)),
        },
        metadata={
            "provider": "servicenow",
            "matched": True,
            "sys_class_name": _resolve(row.get("sys_class_name")),
        },
    )


def _demo_cmdb_fallback(service: str) -> ToolResult:
    """Look the service up in the in-process demo CMDB after a PDI miss.

    Returns ``data=None`` only when the service is unknown in BOTH ServiceNow
    AND the demo table — preserves the agent's existing "Platform On-Call"
    fallback for truly-unknown services.
    """
    info = _demo_cmdb.lookup(service)
    if info is None:
        return ToolResult(
            ok=True,
            data=None,
            metadata={"provider": "servicenow", "matched": False, "fallback": "demo_cmdb"},
        )
    return ToolResult(
        ok=True,
        data={"service": service, "team": info["team"], "runbook": info["runbook"]},
        metadata={"provider": "servicenow", "matched": True, "fallback": "demo_cmdb"},
    )


def _resolve(field: Any) -> str | None:
    """Normalize a ServiceNow field value to a plain string (or ``None``).

    With ``sysparm_display_value=true`` ServiceNow returns scalar fields as
    plain strings but *reference* fields as ``{display_value, link}`` dicts.
    Custom fields can be either depending on their column type. Treat both
    shapes — and the empty-string case — uniformly so callers don't have to.
    """
    if field is None:
        return None
    if isinstance(field, str):
        return field or None
    if isinstance(field, dict):
        v = field.get("display_value")
        return v if isinstance(v, str) and v else None
    return None
