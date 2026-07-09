# `aiops.tools` — tool registry

Every external integration an agent calls — ServiceNow, Jira, PagerDuty, Slack, Prometheus, Loki, Tempo, kubectl, Ansible — registers a *tool* here. Agents reference **capabilities**, not vendors:

```python
from aiops.tools import get_registry

reg = get_registry()
result = reg.call("itsm.incident.create",
                  short_description="payments DB connection pool exhausted",
                  urgency=1)
```

Swap ServiceNow for Jira by changing the active provider in config — no agent code changes:

```python
reg.select_provider("itsm.incident.create", "jira.incident.create")
```

## Capability namespace (Phase 0 baseline)

Capabilities use `<domain>.<noun>.<verb>`. Phase 0 ships mock providers for these; real providers land in Phase 1+.

| Capability | Phase-0 mock | Phase-1+ candidates |
|---|---|---|
| `itsm.incident.create` | `mock.itsm.incident.create` | ServiceNow PDI, Jira |
| `itsm.incident.update` | (Phase 1) | ServiceNow PDI, Jira |
| `observability.metrics.query` | **live: Prometheus** (`prometheus.observability.metrics.query`) | Dynatrace, Datadog |
| `observability.metrics.alerts` | **live: Prometheus** (`prometheus.observability.metrics.alerts`) | Alertmanager |
| `observability.logs.query` | **live: Loki** (`loki.observability.logs.query`) | Splunk, Elastic |
| `observability.traces.services` | **live: Jaeger** (`jaeger.observability.traces.services`) | Tempo, Dynatrace |
| `observability.traces.search` | **live: Jaeger** (`jaeger.observability.traces.search`) | Tempo, Dynatrace |
| `notify.send` | `mock.notify.send` | Slack, Teams, PagerDuty |
| `automation.runbook.execute` | (Phase 2) | Ansible, Azure Automation |
| `kb.search` | (Phase 2) | ServiceNow KB, Confluence, AI Search |

Agents pick capabilities, not products. The `Primary Tool Mapping` and `Secondary Integrations` columns in `docs/Adaptive_AIOps_Agent_Catalog.xlsx` are the source of truth for what each agent depends on.

## Adding a real provider

1. Create `aiops/tools/<vendor>_provider.py`.
2. Decorate each function with `@tool(name=..., capability=..., provider=...)`.
3. Import the module from a wiring point (the bootstrap in `aiops/tools/__init__.py` or a Phase-1 startup hook) so registration runs.

HITL enforcement is automatic — every `get_registry().call(capability, ...)` consults `aiops.policy.get_gate()` for that capability's level before invoking the tool function. REQUIRED-level actions without an approver return `ToolResult(ok=False, ...)` with `metadata["blocked_by"] == "hitl_gate"`. You don't set a flag on the tool; the gate's level mapping (Python dict in Phase 0, OPA query in Phase 2) is the source of truth.
