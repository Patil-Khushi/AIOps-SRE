"""Projects a shared ``IncidentContext`` into ``ContextPackItem`` rows.

This is the smallest seam migrated by the Context Engineering Layer: 8 lines,
2 call sites (`agent.py::_context_item`, called twice from `_build_context_pack`).
Deliberately migrated second — after RCA, before the two higher-risk agents — to
prove the adapter pattern on something trivial before repeating it somewhere a
mistake is expensive.

Byte-identity here is entirely about ``str(result.data)``
-----------------------------------------------------------
``_context_item`` does ``value=str(result.data)`` — the Python ``repr`` of
whatever ``ToolResult.data`` was, posted verbatim into the Slack war-room body,
the JSONL audit log, and ``WarRoomAssembly.context_pack``, which the dashboard
renders. Any normalisation of that payload changes the string. So
``ContextSection.raw`` — the untouched provider payload the context layer already
carries for exactly this reason — is what gets stringified here, never a
processed ``Observation``.

Name collision, deliberately not resolved
------------------------------------------
``ContextPackItem`` and ``WarRoomAssembly.context_pack`` predate this package and
are public, dashboard-visible names. This module does not rename them, and
``aiops/context/`` never uses the word "pack" in its own vocabulary (it is
``IncidentContext``) for exactly this reason — see that package's docstring.
"""

from __future__ import annotations

from agents.notification_assembler.models import ContextPackItem
from aiops.context.models import SectionSpec
from aiops.context.pack import IncidentContext

METRICS_QUERY_ID = "notification.request_rate"
TRACES_QUERY_ID = "notification.recent_traces"


def build_context_request_specs(service: str) -> list[SectionSpec]:
    """The two sections ``_build_context_pack`` needs for ``service``.

    The PromQL here (``http_server_request_duration_count``) is copied verbatim
    from ``agent.py::_build_context_pack`` — a *third* metric-name dialect,
    distinct from both RCA's and Log Correlation's. It is not unified with
    theirs: doing so would change what this specific line of the war-room body
    reports, with nothing in CI to catch a silently different number.
    """
    promql = f'sum(rate(http_server_request_duration_count{{service_name="{service}"}}[5m]))'
    return [
        SectionSpec(source="metrics", query_id=METRICS_QUERY_ID, params={"promql": promql}),
        SectionSpec(
            source="traces",
            query_id=TRACES_QUERY_ID,
            params={"service": service, "lookback": "15m", "limit": 5},
        ),
    ]


def _item_from_section(
    label: str, capability: str, query_id: str, ctx: IncidentContext, source: str
) -> ContextPackItem | None:
    section = ctx.section(source)  # type: ignore[arg-type]
    if not section.status.usable or not section.raw or query_id not in section.raw:
        return None
    # `raw` is the untouched ToolResult.data for this query id — the same object
    # `_context_item` would have stringified from a fresh registry call. Reusing
    # it rather than reformatting is what makes the two paths byte-identical.
    return ContextPackItem(label=label, value=str(section.raw[query_id]), source=capability)


def context_pack_items_from_context(
    ctx: IncidentContext, service: str
) -> list[ContextPackItem] | None:
    """Drop-in replacement for the two ``_context_item`` calls inside
    ``_build_context_pack``. Returns ``None`` when *neither* section was ever
    requested/collected, so the caller can fall back to the live per-item calls
    exactly as it always has — an incident-commander-orchestrated build that
    never asked for notification's sections looks like "no context available",
    not like "both live calls failed".

    When at least one section was attempted, each item is resolved
    independently, exactly matching ``_context_item``'s own behaviour: a failed
    or never-collected lookup already rendered as the single string
    ``"unavailable"`` with no further distinction (unlike RCA's evidence
    categories, nothing here treats an absence as a *positive* claim about the
    system, so there is no need for RCA's per-category live-fallback rule).
    """
    metrics_item = _item_from_section(
        "Request rate (5m)", "observability.metrics.query", METRICS_QUERY_ID, ctx, "metrics"
    )
    traces_item = _item_from_section(
        "Recent traces", "observability.traces.search", TRACES_QUERY_ID, ctx, "traces"
    )
    metrics_requested = ctx.metrics.status.value != "not_requested"
    traces_requested = ctx.traces.status.value != "not_requested"
    if not metrics_requested and not traces_requested:
        return None

    return [
        metrics_item
        or ContextPackItem(
            label="Request rate (5m)", value="unavailable", source="observability.metrics.query"
        ),
        traces_item
        or ContextPackItem(
            label="Recent traces", value="unavailable", source="observability.traces.search"
        ),
    ]
