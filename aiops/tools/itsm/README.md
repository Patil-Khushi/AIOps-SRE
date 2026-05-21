# `aiops.tools.itsm` — ITSM providers

ITSM-backed capabilities for RA-003 Auto-Ticketing. Today this package contains one provider — **ServiceNow** — sitting behind the `aiops.tools` registry. A future Jira provider would land here as a sibling module and register the same capability names; agents would not change.

## Capabilities registered

| Capability | Provider name | HTTP shape |
|---|---|---|
| `itsm.incident.create` | `snow.itsm.incident.create` | `POST /api/now/table/incident` |
| `itsm.incident.update` | `snow.itsm.incident.update` | `PATCH /api/now/table/incident/{sys_id}` |
| `itsm.cmdb.lookup` | `snow.itsm.cmdb.lookup` | `GET /api/now/table/cmdb_ci_service` |

HITL levels are owned by [`aiops.policy.gate`](../../policy/gate.py) — `incident.create` / `incident.update` are **OPTIONAL** (tenant flag), `cmdb.lookup` is **NONE** (read-only). The registry enforces these at the `call()` boundary; the provider never sees blocked calls.

## Setup (in <15 minutes)

### 1. Provision a ServiceNow PDI

1. Sign in / sign up at <https://developer.servicenow.com/> (use a personal email — Zensar SSO won't work).
2. Request an instance. Pick the latest stable release.
3. Wait ~3–5 min for provisioning. Save the **instance URL** (`https://devXXXXXX.service-now.com/`) and the **admin password** shown once.

### 2. Create the `aiops_agent` service account

In the PDI UI as admin:

1. `All` → search **"Users"** → **System Security > Users** → **New**.
2. Fields: User ID `aiops_agent`, first/last name as you like, **Web service access only ✅**, **Internal Integration User ✅**.
3. **Set the password via the "Set Password" related link** on the user form — *not* the `Password` field on the form. The Table API's `user_password` field is silently dropped in some PDI releases (see [`docs/llm-access.md`](../../../docs/llm-access.md) for the equivalent ServiceNow quirk pattern, and issue [#43](https://github.com/UbiquotousPanda/AIops/issues/43)).
4. Save the user record.
5. Open the user → **Roles** related list → add **`itil`** → Save.

Quick verification from PowerShell — fill in `.env` (next section) first, then:

```powershell
.\scripts\verify_snow_creds.ps1
```

That reads `AIOPS_SERVICENOW_*` from `.env`, probes `/api/now/table/incident`, and prints OK/401. Equivalent ad-hoc curl if you prefer:

```powershell
$u = "aiops_agent"; $p = "<the-password>"; $url = "https://devXXXXXX.service-now.com"
$b64 = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("$($u):$($p)"))
curl.exe -H "Authorization: Basic $b64" -H "Accept: application/json" `
  "$url/api/now/table/incident?sysparm_limit=1"
```

Expect a 200 with a `result` array.

### 3. Fill in `.env`

```ini
AIOPS_SERVICENOW_INSTANCE_URL=https://devXXXXXX.service-now.com
AIOPS_SERVICENOW_USER=aiops_agent
AIOPS_SERVICENOW_PASSWORD=<password>
AIOPS_SERVICENOW_TIMEOUT=15
AIOPS_USE_MOCK_ITSM=false
```

Setting `AIOPS_USE_MOCK_ITSM=false` is what tells the registry to let the real ServiceNow client take over `itsm.cmdb.lookup` — without it, the static mock in [`aiops/tools/mock_providers.py`](../mock_providers.py) keeps winning.

## Capability contracts

### `itsm.incident.create`

```python
reg.call(
    "itsm.incident.create",
    short_description="payment-service CPU > 90% for 5m",
    urgency=2,              # 1=High, 2=Medium, 3=Low
    description="...",      # optional
    assignment_group="...", # optional — sys_id or display name
    caller_id="...",        # optional
    category="...",         # optional
)
# ok=True, data={"sys_id", "number", "state", "short_description", "urgency"}
```

### `itsm.incident.update`

`fields` is an **explicit dict**, not `**kwargs` — the registry filters call kwargs against the function signature, so a var-kwarg parameter would lose every field.

```python
reg.call(
    "itsm.incident.update",
    sys_id="45175e8a...",
    fields={"state": "2", "work_notes": "investigating"},
)
# ok=True, data={"sys_id", "number", "state"}
```

### `itsm.cmdb.lookup`

```python
reg.call("itsm.cmdb.lookup", service="payment-service")
# Hit:    ok=True, data={"service", "team", "runbook"}
# Miss:   ok=True, data=None, metadata={"matched": false}
```

The runbook field is sourced from `cmdb_ci_service.{AIOPS_SERVICENOW_RUNBOOK_FIELD}` (default `u_runbook_url`). Stock PDIs don't ship with this column, so the value is normally `None` — the agent's "Platform On-Call" routing fallback handles that case.

## Mock co-existence

Capability ownership is decided by the registry's `setdefault` on first registration. Import order in [`agents/alert_triage/agent.py`](../../../agents/alert_triage/agent.py) puts the mock module before this one, so the mock wins by default. The mock for `itsm.cmdb.lookup` is gated on `AIOPS_USE_MOCK_ITSM` — when that flag is `false`, the mock skips its `@tool` registration entirely and this provider takes the capability.

The mock for `itsm.incident.create` is *unconditional* (the smoke test [`tests/test_smoke.py::test_tool_registry_dispatches_by_capability`](../../../tests/test_smoke.py) exercises it). When the Auto-Ticketing agent ships, it should call `select_provider("itsm.incident.create", "snow.itsm.incident.create")` to flip the active provider — that's the documented swap pattern, not a code change here.

## Revoking credentials

1. In the PDI UI, open the `aiops_agent` user record.
2. Toggle **Active** to false (preserves audit trail) **or** click **Delete**.
3. Remove the values from every developer's `.env`.

If the admin password is in any developer's `.env` (the workaround we used while issue #43 is open), rotate the admin password from `User profile → Change password` in the PDI and update `.env` accordingly.
