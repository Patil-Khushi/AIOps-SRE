"""Collectors for the three sources that are already chained seams.

``topology``, ``incident_history`` and ``deployments`` are not single capabilities.
Each is a provider chain that already owns its own guarding, breakers, caching,
fallback ordering and attempt log — ``aiops/tools/topology/resolver.py``,
``incident_history/retriever.py`` and ``change_context/collector.py``.

So these collectors are thin pass-throughs, and deliberately so. Wrapping an
already-guarded seam in a second ``resilience.guard`` would nest two timeouts and
two retry budgets: a 3-second outer bound around an inner chain that is itself
allowed 3 seconds per provider across several providers, so the outer timeout would
fire mid-chain and report ``FAILED`` for a chain that was working through its tiers
normally. The inner seam's own protections are the correct ones; this layer's job is
only to translate their vocabulary into ``SectionStatus`` and keep their provenance.

That translation is the real work here. All three seams already make the
four-way distinction this package needs, using three different sets of names
(``RESOLVED``/``MATCHED``/``COLLECTED`` for the success case), so each collector maps
one vocabulary onto ``SectionStatus`` and preserves the seam's own note verbatim
rather than inventing a new wording — an operator-facing string that changes meaning
between two layers is worse than no string.
"""

from __future__ import annotations

import logging
from typing import Any

from aiops.context.models import SectionSpec, SectionStatus
from aiops.context.pack import ContextSection, SourceProvenance

logger = logging.getLogger(__name__)


def _section(
    *,
    status: SectionStatus,
    provider: str,
    query_id: str,
    payload: Any,
    latency_ms: float = 0.0,
    error: str | None = None,
    note: str | None = None,
) -> ContextSection:
    return ContextSection(
        status=status,
        provenance=SourceProvenance(
            provider=provider,
            status=status,
            latency_ms=latency_ms,
            error=error,
            coverage_note=note,
        ),
        raw={query_id: payload} if status.usable else None,
    )


def _failed(source: str, provider: str, exc: Exception) -> ContextSection:
    """A seam raised despite documenting that it does not.

    Every seam here promises never to raise, and each is defensive internally. This
    branch exists anyway because ``collect()`` must not raise either, and a bug one
    layer down must cost this section rather than the whole context build.
    """
    logger.debug("context collector for %s raised", source, exc_info=True)
    return _section(
        status=SectionStatus.FAILED,
        provider=provider,
        query_id="",
        payload=None,
        error=f"{type(exc).__name__}: {exc}",
    )


class TopologyCollector:
    """Dependencies for a service, via ``aiops.tools.topology.resolve``.

    Fills the ``topology`` section. Uses the resolver chain rather than the
    ``itsm.cmdb.dependencies`` capability directly, because the chain is what
    expresses "try OTel, then the CMDB, then the static table" — a preference order
    the tool registry's one-provider-per-capability model cannot represent.
    """

    name = "topology"
    source = "topology"

    def collect(self, spec: SectionSpec, correlation_id: str) -> ContextSection:
        service = str(spec.params.get("service") or "")
        if not service:
            return _section(
                status=SectionStatus.UNAVAILABLE,
                provider=self.name,
                query_id=spec.query_id,
                payload=None,
                note="no service given",
            )
        try:
            from aiops.tools.topology import resolve

            resolution = resolve(service)
        except Exception as exc:
            return _failed(self.source, self.name, exc)

        winner = resolution.winning_provider or self.name
        attempts = [
            {
                "provider": attempt.provider,
                "status": str(attempt.status),
                "dependencies": list(attempt.dependencies),
                "error": attempt.error,
                "note": attempt.note,
                "latency_ms": attempt.latency_ms,
                "cached": attempt.cached,
            }
            for attempt in resolution.attempts
        ]
        # The chain's own view of "did anyone answer". EMPTY when every tier was
        # reachable and none knew about this service — a real answer on a stock
        # ServiceNow PDI, where the demo services have no CI records at all.
        status = SectionStatus.COLLECTED if resolution.resolved else SectionStatus.EMPTY
        note = None
        if not resolution.resolved:
            note = "no provider resolved dependencies for this service"
        if resolution.budget_exhausted:
            note = "topology budget exhausted before every tier was tried"
        return _section(
            status=status,
            provider=winner,
            query_id=spec.query_id,
            payload={
                "service": service,
                "dependencies": list(resolution.dependencies),
                "winning_provider": resolution.winning_provider,
                "attempts": attempts,
            },
            note=note,
        )


class IncidentHistoryCollector:
    """Similar past incidents, via ``aiops.tools.incident_history.search_similar``.

    Fills the ``incident_history`` section. ``search_similar`` returns *every*
    provider attempt in order rather than only the winner, so the payload keeps all
    of them: which tier answered is diagnostically useful, and a mock tier matching
    where the embedding tier did not is a fact worth being able to see.
    """

    name = "incident_history"
    source = "incident_history"

    def collect(self, spec: SectionSpec, correlation_id: str) -> ContextSection:
        try:
            from aiops.tools.incident_history import (
                RetrievalQuery,
                RetrievalStatus,
                search_similar,
            )

            query = RetrievalQuery(
                service=str(spec.params.get("service") or ""),
                signatures=list(spec.params.get("signatures") or []),
                services_involved=list(spec.params.get("services_involved") or []),
                topology=list(spec.params.get("topology") or []),
                limit=int(spec.params.get("limit") or 5),
                min_similarity=float(spec.params.get("min_similarity") or 0.1),
            )
            results = search_similar(query)
        except Exception as exc:
            return _failed(self.source, self.name, exc)

        matched = [r for r in results if r.matched]
        winner = matched[0] if matched else None
        attempts = [
            {
                "provider": r.provider,
                "status": str(r.status),
                "error": r.error,
                "note": r.note,
                "latency_ms": r.latency_ms,
                "corpus_size": r.corpus_size,
                "match_count": len(r.matches),
            }
            for r in results
        ]

        if winner is None:
            # Separate "no tier could be asked" from "every tier was asked and
            # nothing was similar enough". The second is a real answer — this
            # incident is novel — and a consumer may legitimately reason from it.
            # The first is a blind spot and must never be presented as the second.
            #
            # RetrievalStatus.EMPTY is the seam's own word for "queried
            # successfully, found nothing", so it is the only status that licenses
            # SectionStatus.EMPTY here. UNAVAILABLE and FAILED do not.
            searched = any(r.status is RetrievalStatus.EMPTY for r in results)
            status = SectionStatus.EMPTY if searched else SectionStatus.UNAVAILABLE
            note = (
                "searched; no past incident scored above the similarity floor"
                if searched
                else "no incident-history provider was available"
            )
            return _section(
                status=status,
                provider=self.name,
                query_id=spec.query_id,
                payload={"matches": [], "attempts": attempts},
                note=note,
            )

        return _section(
            status=SectionStatus.COLLECTED,
            provider=winner.provider,
            query_id=spec.query_id,
            payload={
                "matches": [m.model_dump(mode="json") for m in winner.matches],
                "attempts": attempts,
            },
            latency_ms=winner.latency_ms,
        )


class DeploymentsCollector:
    """Recent deployments and commits, via ``change_context.collect_change_context``.

    Fills the ``deployments`` section. That seam fans out over GitHub, Kubernetes
    rollouts and feature flags and *merges* the results rather than taking the first
    that answers — so unlike topology there is no single winning provider, and the
    section records which sources contributed and which could not.

    A deploy minutes before onset is the most common real-world root cause and the
    one signal metrics, logs and traces structurally cannot provide, which is why
    this is a first-class section rather than enrichment metadata.
    """

    name = "change_context"
    source = "deployments"

    def collect(self, spec: SectionSpec, correlation_id: str) -> ContextSection:
        service = str(spec.params.get("service") or "")
        start = spec.params.get("window_start")
        end = spec.params.get("window_end")
        if not service or start is None or end is None:
            return _section(
                status=SectionStatus.UNAVAILABLE,
                provider=self.name,
                query_id=spec.query_id,
                payload=None,
                note="no service or window given",
            )
        try:
            from aiops.tools.change_context import collect_change_context

            context = collect_change_context(service, start, end)
        except Exception as exc:
            return _failed(self.source, self.name, exc)

        if not context.sources_collected:
            return _section(
                status=SectionStatus.UNAVAILABLE,
                provider=self.name,
                query_id=spec.query_id,
                payload=None,
                note=context.coverage_note or "no change-context provider was available",
            )

        status = SectionStatus.COLLECTED if context.records else SectionStatus.EMPTY
        return _section(
            status=status,
            provider=",".join(context.sources_collected),
            query_id=spec.query_id,
            payload={
                "records": [r.model_dump(mode="json") for r in context.records],
                "sources_collected": list(context.sources_collected),
                "sources_unavailable": list(context.sources_unavailable),
            },
            note=context.coverage_note,
        )
