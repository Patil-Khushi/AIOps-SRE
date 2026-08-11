"""Projects a shared ``IncidentContext`` into RCA's own evidence shape.

Before / after
--------------
::

    Before:                          After:
    RCA Agent                        RCA Agent
      |                                |
    _evidence.gather(service)        if context available:
      | (8 registry.call sites,          evidence_from_context(ctx, service)  <- this file
      |  each try/except individually) else:
      |                                  _evidence.gather(service)  <- unchanged
    render(observed) -> prompt         |
                                      render(observed) -> prompt   <- UNCHANGED either way

Why this file exists rather than reusing ``evidence.py``'s functions directly
------------------------------------------------------------------------------
``evidence.py`` already has a dependency-injection seam (``Backend``,
``QueryFn``) built for exactly this: every category-gathering function takes its
row source as a parameter defaulting to the live registry call. This module
supplies the other implementation of that seam — one that reads rows out of an
already-built ``IncidentContext`` instead of querying Prometheus/Loki again — so
``gather()``'s floors, its NaN guard, its key-insertion order and every format
string in ``render()`` run unchanged regardless of which backend answered.  The
alternative, reimplementing all eight evidence categories against context
payloads, would mean a second copy of ``f"pod {pod}: cpu={cores:.2f} cores
(limit 1)"`` and of the 20%/80% reporting floors — copies that would drift the
first time someone edited the original.

What is NOT migrated, and why
------------------------------
Two of the eight categories still reach the live registry even when a context is
supplied: ``recent_changes`` (inside ``gather``) and ``_fetch_change_evidence``
(a second, separate query in ``agent.py``). Both call ``scm.commit.history``
directly with a specific ``path``/``since``/``limit`` — a capability-direct,
per-service-path query. The Context Engineering Layer's ``deployments`` section
is bound to ``aiops.tools.change_context.collect_change_context``, which fans
out over GitHub *and* Kubernetes rollouts *and* feature flags and merges them
into one union for a whole incident window — a different shape, answering a
different question, with no per-service path scoping. Forcing RCA's path-scoped
commit query onto that union would silently change which commits RCA sees.
Rather than bolt on a mismatch, both SCM queries are left exactly as they were;
``tests/test_retrieval_call_sites.py`` still tracks them as direct call sites for
this reason.
"""

from __future__ import annotations

from typing import Any

from agents.rca_agent import evidence as _evidence
from aiops.context.models import SectionSpec
from aiops.context.pack import IncidentContext
from aiops.tools.registry import ToolResult

ALWAYS_KEYS = frozenset({"firing_alerts", "error_breakdown", "pod_state", "resource_saturation"})
"""The four categories ``evidence.render()`` always prints — as a real finding when
present, as an explicit ``NONE — <explanation>`` line when absent. That ``NONE``
line is a claim about the world ("this was checked and is absent"), which is only
true when the section was actually queried. See ``evidence_from_context``."""


class ContextBackend:
    """``evidence.Backend`` sourced from an already-built ``IncidentContext``.

    Implements ``query``, ``alerts`` and ``logs`` by reading rows out of
    ``ctx.metrics``/``ctx.logs``. ``commits`` is deliberately **not** sourced from
    the context — see the module docstring — and degrades to the same live call
    ``evidence.py`` has always made, so ``recent_changes`` behaves identically
    whether or not a context is supplied.
    """

    def __init__(self, ctx: IncidentContext) -> None:
        self._metrics_raw = (ctx.metrics.raw or {}) if ctx.metrics.status.usable else {}
        self._logs_raw = (ctx.logs.raw or {}) if ctx.logs.status.usable else {}

    def query(self, promql: str) -> list[dict[str, Any]]:
        """Rows for one PromQL string, keyed exactly as the request built them.

        The context builder stores a query's raw payload under its ``query_id``
        (``aiops/context/collectors/base.py``), so ``build_context_request_specs``
        below sets ``query_id`` to the PromQL text itself — the same string
        ``evidence.py``'s functions already pass around — rather than inventing a
        second naming scheme this module alone would need to keep in sync.
        """
        payload = self._metrics_raw.get(promql)
        if not isinstance(payload, dict):
            return []
        rows = payload.get("results")
        if rows is None:
            rows = payload.get("result")
        return rows if isinstance(rows, list) else []

    def alerts(self) -> ToolResult:
        payload = self._metrics_raw.get(_evidence.ALERTS_QUERY_ID)
        if not isinstance(payload, dict):
            return ToolResult(ok=False, error="not collected", metadata={"missing_provider": True})
        return ToolResult(ok=True, data=payload, metadata={})

    def logs(self, service: str) -> ToolResult:
        payload = self._logs_raw.get(_evidence.LOGS_QUERY_ID)
        if not isinstance(payload, dict):
            return ToolResult(ok=False, error="not collected", metadata={"missing_provider": True})
        return ToolResult(ok=True, data=payload, metadata={})

    def commits(self, path: str | None, limit: int) -> Any:
        return _evidence.live_commits(path, limit)


def build_context_request_specs(
    service: str, *, window_start: Any, window_end: Any
) -> list[SectionSpec]:
    """The sections RCA needs from the Context Engineering Layer for ``service``.

    Every PromQL query ``gather`` will ever issue (via ``required_promql_queries``,
    so the two cannot drift apart), plus the alerts and recent-logs lookups. Deploy
    history is deliberately absent — see the module docstring.
    """
    specs = [
        SectionSpec(source="metrics", query_id=promql, params={"promql": promql})
        for promql in _evidence.required_promql_queries()
    ]
    specs.append(
        SectionSpec(
            source="metrics",
            query_id=_evidence.ALERTS_QUERY_ID,
            capability="observability.metrics.alerts",
        )
    )
    specs.append(
        SectionSpec(
            source="logs",
            query_id=_evidence.LOGS_QUERY_ID,
            params={
                "service": service,
                "start": window_start,
                "end": window_end,
                "limit": 200,
            },
        )
    )
    return specs


def _live_always_keys(missing: frozenset[str]) -> dict[str, list[str]]:
    """Re-run the always-print categories that a missing metrics section could not
    have answered, straight against the live registry. See ``evidence_from_context``
    for why this is the one place fallback happens per-category rather than
    all-or-nothing."""
    live: dict[str, list[str]] = {}
    sources: dict[str, Any] = {
        "firing_alerts": _evidence.firing_alerts,
        "error_breakdown": _evidence.error_breakdown,
        "pod_state": _evidence.pod_state,
        "resource_saturation": _evidence.resource_saturation,
    }
    for key in missing:
        try:
            if rows := sources[key]():
                live[key] = rows
        except Exception:  # pragma: no cover - gather()'s own functions never raise
            pass
    return live


def evidence_from_context(ctx: IncidentContext, service: str) -> dict[str, list[str]]:
    """Drop-in replacement for ``_evidence.gather(service)``, sourced from ``ctx``.

    Returns ``{}`` when nothing was collectible anywhere — from the context or from
    the live fallback below — so ``render({})`` produces the exact "no live
    evidence" branch ``agent.py`` already has. There is deliberately no early
    return for "the context has nothing usable": an earlier version short-circuited
    there, which felt like an optimisation but silently skipped the per-category
    fallback below in exactly the case that fallback exists for — a whole-backend
    outage, where both ``ctx.metrics`` and ``ctx.logs`` are unusable.

    Per-category fallback for the four ``ALWAYS_KEYS``
    ---------------------------------------------------
    ``render()`` prints an explicit ``NONE — this signal was checked and was
    absent`` line for these four when they are absent from the evidence dict, and
    the RCA prompt instructs the model to treat that as *positive evidence against*
    any cause that would produce the signal. That sentence is only true when the
    category was actually queried.

    If ``ctx.metrics`` is not ``usable`` — nobody could reach the backend at all —
    then an always-key missing from ``gather()``'s output does not mean "checked,
    found nothing"; it means "never checked". Presenting it as ``NONE`` in that case
    would hand the model a false negative dressed up as positive evidence, which is
    precisely the failure mode the category's own docstring in ``evidence.py``
    warns about. So when the metrics section is unusable, the four always-keys are
    re-queried directly against the live registry — the same call ``gather()`` would
    have made with no context at all — rather than silently rendered as ruled out.

    When ``ctx.metrics`` *is* usable, an individual query within it can still have
    failed while a sibling query succeeded (the section's status is the strongest of
    several merged queries — see ``aiops/context/builder.py::_merge``). That is
    accepted here without a per-query fallback: it has exactly the same fidelity the
    legacy ``_q()`` path already has today, where one failed PromQL call just
    returns ``[]`` and the category renders as ``NONE`` — so serving the context
    path degrades no further than the code it replaces.
    """
    observed = _evidence.gather(service, ContextBackend(ctx))
    if not ctx.metrics.status.usable:
        missing_always = ALWAYS_KEYS - observed.keys()
        observed = {**observed, **_live_always_keys(missing_always)}
    return observed
