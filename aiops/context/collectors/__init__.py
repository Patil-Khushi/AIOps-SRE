"""The collector registry — which collector fills which section.

Stage 1 of the pipeline. Everything here is I/O; every later stage is pure.

Eight of the eleven sources are one ``CapabilityCollector`` each, configured by
composition rather than subclassing. The three that are already provider chains
live in ``seams.py``.

The emptiness predicates are the part worth reading
---------------------------------------------------
Each collector needs to know what its provider's payload looks like when the answer
is genuinely nothing, because ``EMPTY`` and ``FAILED`` mean opposite things to a
consumer and the providers disagree about how they say it:

    metrics   {"results": []}          logs     {"streams": []}
    alerts    {"alerts": []}           traces   {"trace_count": 0}
    commits   {"commits": []}          deps     {"dependencies": []}

Getting one of these wrong is not a cosmetic bug. RCA renders an explicit
"NONE — this signal was checked and was absent" line for an empty category and
instructs the model to treat it as positive evidence *against* any cause that would
have produced that signal. Mislabel an unreachable Prometheus as ``EMPTY`` and the
model is told, in so many words, that a cause has been ruled out when nothing was
ever checked.

The predicates are pinned by ``tests/test_context_collectors.py`` against the real
provider payload shapes, so a provider that changes its schema fails a test rather
than silently starting to report every section as collected.

Chain configuration
-------------------
``AIOPS_CONTEXT_COLLECTORS`` selects which collectors are eligible, and unknown
names are **returned rather than logged and dropped** — the convention
``change_context/collector.py`` established after a typo'd provider name silently
produced a context that looked complete. A caller can tell the difference between
"that source was not requested" and "that source name does not exist".
"""

from __future__ import annotations

import os
from typing import Any

from aiops.context.collectors.base import (
    CapabilityCollector,
    Collector,
    not_requested,
    unavailable,
)
from aiops.context.collectors.seams import (
    DeploymentsCollector,
    IncidentHistoryCollector,
    TopologyCollector,
)

__all__ = [
    "CapabilityCollector",
    "Collector",
    "DeploymentsCollector",
    "IncidentHistoryCollector",
    "TopologyCollector",
    "available_collectors",
    "collector_for",
    "not_requested",
    "resolve_chain",
    "unavailable",
]


def _empty_list_at(key: str) -> Any:
    """Predicate: payload is empty when ``data[key]`` is an empty sequence.

    Treats a missing key as *not* empty. A payload that does not carry the expected
    key is a schema surprise, not an answer about the world, and the safer reading of
    a surprise is "we got something we do not understand" rather than "this signal
    was checked and was absent".
    """

    def predicate(data: Any) -> bool:
        if not isinstance(data, dict) or key not in data:
            return data is None
        value = data.get(key)
        return isinstance(value, list | tuple) and len(value) == 0

    return predicate


def _empty_when_zero(key: str) -> Any:
    """Predicate for a payload that reports its own count (Jaeger's ``trace_count``)."""

    def predicate(data: Any) -> bool:
        if not isinstance(data, dict) or key not in data:
            return data is None
        try:
            return int(data[key]) == 0
        except (TypeError, ValueError):
            return False

    return predicate


def _build_collectors() -> dict[str, Collector]:
    """One collector per ``Source``.

    Constructed fresh on each call rather than held in a module-level dict: a
    ``CapabilityCollector`` validates its capability against the denylist in
    ``__init__``, and doing that at import time would make an import failure out of
    what should be a clear error at the call site. It is also cheap — these objects
    hold three strings and a function.
    """
    collectors: list[Collector] = [
        CapabilityCollector(
            name="prometheus",
            source="metrics",
            capability="observability.metrics.query",
            is_empty=_empty_list_at("results"),
        ),
        CapabilityCollector(
            name="loki",
            source="logs",
            capability="observability.logs.query",
            is_empty=_empty_list_at("streams"),
        ),
        CapabilityCollector(
            name="jaeger",
            source="traces",
            capability="observability.traces.search",
            is_empty=_empty_when_zero("trace_count"),
        ),
        CapabilityCollector(
            name="k8s_events",
            source="k8s_events",
            capability="observability.events.query",
            is_empty=_empty_list_at("events"),
        ),
        CapabilityCollector(
            name="cmdb_dependencies",
            source="dependencies",
            capability="itsm.cmdb.dependencies",
            is_empty=_empty_list_at("dependencies"),
        ),
        CapabilityCollector(
            name="cmdb",
            source="cmdb",
            capability="itsm.cmdb.lookup",
            # No list to be empty — a CMDB lookup either resolves ownership or it
            # does not, and the mock deliberately falls back to a default team so
            # an agent always has somewhere to route. Any payload is an answer.
        ),
        CapabilityCollector(
            name="oncall",
            source="oncall",
            capability="oncall.schedule.lookup",
        ),
        CapabilityCollector(
            name="resolvers",
            source="runbooks",
            capability="incident.resolvers.lookup",
            is_empty=_empty_list_at("resolvers"),
        ),
        TopologyCollector(),
        IncidentHistoryCollector(),
        DeploymentsCollector(),
    ]
    return {c.source: c for c in collectors}


def available_collectors() -> dict[str, Collector]:
    """Every collector, keyed by the section it fills."""
    return _build_collectors()


def collector_for(source: str) -> Collector | None:
    """The collector for one section, or ``None`` if no collector serves it."""
    return _build_collectors().get(source)


def resolve_chain() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(known, unknown)`` collector names from ``AIOPS_CONTEXT_COLLECTORS``.

    Unset means every collector is eligible — the useful default, since a caller
    already scopes what it wants through its ``ContextRequest`` and this variable
    exists to *disable* a source (an unreachable Loki in a demo, say), not to opt
    into each one.

    Unknown names are returned, not swallowed. ``change_context/collector.py``
    documents why: a chain of nothing but typos would otherwise report a context
    that had asked no one as though it were complete, and false completeness is
    worse than a visible gap.
    """
    raw = os.environ.get("AIOPS_CONTEXT_COLLECTORS", "").strip()
    known_sources = set(_build_collectors())
    if not raw:
        return tuple(sorted(known_sources)), ()

    requested = [part.strip() for part in raw.split(",") if part.strip()]
    known = tuple(name for name in requested if name in known_sources)
    unknown = tuple(name for name in requested if name not in known_sources)
    return known, unknown
