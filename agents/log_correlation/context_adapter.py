"""Projects a shared ``IncidentContext`` into Log Correlation's own fetch shape.

Same dependency-injection pattern as ``agents/rca_agent/context_adapter.py`` and
``agents/notification_assembler/context_adapter.py``: ``agent.py``'s three fetch
functions (``_fetch_logs``, ``_fetch_traces``, ``_fetch_metrics``) each already
take an optional ``fetch`` callable defaulting to the live registry call. This
module supplies the other implementation of that callable — one that reads a
``ToolResult``-shaped answer out of an already-built ``IncidentContext`` — so
every downstream line (the stream/values walk, the fingerprinting, the
timestamp-fallback logic, every trace string) is the *same code* regardless of
which one answered.

``CorrelationResult`` is not touched
-------------------------------------
This migration only replaces the three ``_fetch_*`` internals. The output model,
its ``extra="forbid"`` fields, and the ``demo/dashboard`` TypeScript contract
built on it are untouched — provenance goes into the existing free-form
``decision_trace: list[str]``, which already carries operator-facing strings
like ``"logs: 3 matching line(s) from loki"``.

The ``ThreadPoolExecutor`` fan-out in ``correlate()`` is kept
---------------------------------------------------------------
Even though a context-sourced fetch does no I/O, the executor stays: the three
``_fetch_*`` calls append to one shared ``decision_trace`` list from separate
threads today, so trace-line *ordering* on the live path is already
non-deterministic and no golden covers it (goldens force
``force_synthetic=True``). Making the context path synchronous would make that
ordering deterministic — a real behavior change, just not one this migration
is choosing to make.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agents.log_correlation.agent import _metrics_promql
from agents.log_correlation.models import CorrelationInput
from aiops.context.models import SectionSpec
from aiops.context.pack import ContextSection, IncidentContext
from aiops.tools.registry import ToolResult

LOGS_QUERY_ID = "log_correlation.logs"
TRACES_QUERY_ID = "log_correlation.traces"


def build_context_request_specs(payload: CorrelationInput) -> list[SectionSpec]:
    """The three sections ``correlate()`` needs for one incident.

    Params mirror the live calls exactly: ISO-string window for logs (Loki's
    provider normalises both ISO strings and ``datetime`` objects, but the
    fingerprint must match what the request actually asked for), the same
    lookback string for traces, and the same PromQL for metrics.
    """
    svc = payload.service.lower().strip()
    return [
        SectionSpec(
            source="logs",
            query_id=LOGS_QUERY_ID,
            params={
                "service": svc,
                "start": payload.window.start.isoformat(),
                "end": payload.window.end.isoformat(),
                "limit": 200,
            },
        ),
        SectionSpec(
            source="traces",
            query_id=TRACES_QUERY_ID,
            params={"service": svc, "lookback": _lookback_param(payload), "limit": 10},
        ),
        SectionSpec(
            source="metrics",
            query_id=_metrics_promql(payload),
            params={"promql": _metrics_promql(payload)},
        ),
    ]


def _lookback_param(payload: CorrelationInput) -> str:
    from agents.log_correlation.agent import _lookback_str

    return _lookback_str(payload.window)


def _result_from_section(section: ContextSection, query_id: str) -> ToolResult:
    """Reconstruct the ``ToolResult`` a live call would have returned, from an
    already-built section.

    ``section.raw`` only holds the payload for a *usable* (``COLLECTED``/``EMPTY``)
    query, so a failure has nothing to read back there — the exact error text lives
    in ``provenance.error`` instead, which the collector copies verbatim from the
    original ``ToolResult.error`` (``aiops/context/collectors/base.py``). Rebuilding
    from that is what makes a reconstructed failure produce the identical trace
    string (``f"logs: loki error ({res.error})"``) the live path would have.
    """
    if section.status.usable and section.raw and query_id in section.raw:
        return ToolResult(ok=True, data=section.raw[query_id], metadata={})
    return ToolResult(
        ok=False,
        error=section.provenance.error,
        metadata={"missing_provider": section.status.value == "unavailable"},
    )


def context_logs_fetch(ctx: IncidentContext) -> Callable[[CorrelationInput], Any]:
    return lambda _payload: _result_from_section(ctx.logs, LOGS_QUERY_ID)


def context_traces_fetch(ctx: IncidentContext) -> Callable[[CorrelationInput], Any]:
    return lambda _payload: _result_from_section(ctx.traces, TRACES_QUERY_ID)


def context_metrics_fetch(ctx: IncidentContext) -> Callable[[CorrelationInput], Any]:
    def _fetch(payload: CorrelationInput) -> Any:
        return _result_from_section(ctx.metrics, _metrics_promql(payload))

    return _fetch
