"""Projects a shared ``IncidentContext`` into Alert Triage's own fetch shape.

Migrated last, deliberately. Alert Triage is the highest-risk agent in this
migration for two independent reasons:

* It is the **sole member** of the eval harness's ``_TRUTH_FILE_RUNNABLE_AGENTS``
  — the only agent the 27 truth-file scenarios actually exercise — so a
  regression here moves every one of those buckets, not just its own golden.
* Its ``decision_trace`` is persisted to SQLite and later reconstructed by
  ``_verdict_from_row``; ``tests/test_alert_triage_idempotency.py`` asserts the
  reconstructed trace is byte-**equal** to a fresh one. A single reworded
  ``metrics_ctx[...]`` line breaks that test.

Same dependency-injection pattern as the three agents before it
------------------------------------------------------------------
``_fetch_metric_context`` and ``_fetch_trace_context`` in ``agent.py`` each take
callables (``query_fn``/``capability_available``, ``search_fn``) defaulting to
the live registry. This module supplies context-sourced implementations of
those callables, so the thread pool, the per-query error text, and the results
dict shape are the *same code* regardless of source.

``Alert`` is not touched
-------------------------
``Alert`` is a webhook contract whose fields leak into the dedup cluster key and
the ServiceNow ticket description — not a place to attach a context. ``context``
travels as a keyword argument to ``triage()`` instead.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agents.alert_triage.agent import _build_promql_queries, trace_context_candidates
from agents.alert_triage.models import Alert
from aiops.context.models import SectionSpec
from aiops.context.pack import ContextSection, IncidentContext
from aiops.tools.registry import ToolResult


def build_context_request_specs(alert: Alert) -> list[SectionSpec]:
    """Every section Alert Triage's live path could ask for, for one alert.

    Metrics: one spec per named PromQL query (``_build_promql_queries`` — the
    same source of truth the live path itself builds from, so a query added
    there is requested here automatically). Traces: all three candidate service
    names, because the context layer collects eagerly while the live loop
    short-circuits on the first hit — asking for fewer than three could miss
    the one that would have won.
    """
    queries = _build_promql_queries(alert)
    specs = [
        SectionSpec(source="metrics", query_id=promql, params={"promql": promql})
        for promql in queries.values()
    ]
    specs.extend(
        SectionSpec(
            source="traces",
            query_id=candidate,
            params={"service": candidate, "lookback": "15m", "limit": 5},
        )
        for candidate in trace_context_candidates(alert)
    )
    return specs


def _result_from_section(section: ContextSection, query_id: str) -> ToolResult:
    """Reconstruct the ``ToolResult`` a live call would have returned.

    Same reasoning as the other three agents' adapters: ``section.raw`` only
    holds a usable query's payload, so a failure's error text is read back from
    ``provenance.error`` — copied verbatim from the original ``ToolResult.error``
    by the collector — which is what makes a reconstructed failure produce the
    identical ``f"prometheus error ({res.error})"`` trace line.
    """
    if section.status.usable and section.raw and query_id in section.raw:
        return ToolResult(ok=True, data=section.raw[query_id], metadata={})
    return ToolResult(
        ok=False,
        error=section.provenance.error,
        metadata={"missing_provider": section.status.value == "unavailable"},
    )


def context_metrics_capability_available(ctx: IncidentContext) -> Callable[[], bool]:
    """Whether the metrics *section* was ever reachable — the context-path
    equivalent of the live pre-flight ``by_capability`` probe.

    ``NOT_REQUESTED``/``UNAVAILABLE`` map to "not registered", matching what the
    live probe would have found for an unconfigured capability. ``COLLECTED``/
    ``EMPTY``/``FAILED`` all mean something *did* answer — even a per-query
    failure is a real attempt, not an absent capability — so they return
    ``True`` and let the per-query error text (already carried in
    ``section.raw``'s absence + ``provenance.error``) speak for the specifics.
    """
    available = ctx.metrics.status.value not in ("not_requested", "unavailable")
    return lambda: available


def context_metric_query(ctx: IncidentContext) -> Callable[[str], Any]:
    return lambda promql: _result_from_section(ctx.metrics, promql)


def context_trace_search(ctx: IncidentContext) -> Callable[[str], Any]:
    return lambda candidate: _result_from_section(ctx.traces, candidate)
