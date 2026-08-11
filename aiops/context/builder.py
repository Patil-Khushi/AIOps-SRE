"""``ContextBuilder`` — the eight-stage pipeline that produces an ``IncidentContext``.

    1. Collect     (impure — the only stage that touches a backend)
    2. Normalize   -> Observation objects, one vocabulary from eleven schemas
    3. Correlate   -> cross-source agreement + topology relation
    4. Rank        -> deterministic, explainable relevance ordering
    5. Enrich      -> ownership and recent-change metadata
    6. Redact      -> secrets and PII scrubbed before anything reaches a prompt
    7. Budget      -> projected to a consumer's token allowance (opt-in)
    8. Assemble    -> frozen into an IncidentContext

Stages 2-8 are pure functions over data structures. That is the design's main
payoff: everything except stage 1 is testable with no mocks, no network, and no
clock, and a bug in ranking or redaction can be reproduced from a literal.

Concurrency — why this owns an executor instead of leaning on ``resilience``
---------------------------------------------------------------------------
``resilience.guard`` runs each guarded call in one process-wide
``ThreadPoolExecutor(max_workers=8)`` shared with the topology, incident-history and
change-context seams. A collector calls ``guard``, so if this builder fanned out
eleven collectors at once there would be eleven submissions against those eight
slots.

The failure that causes is quiet. ``guard`` reports a call that never got a slot as
``starved``, and ``starved`` deliberately does **not** trip a breaker and does not
count as an attempt — the flag exists to say "some *other* seam is hogging the pool",
which is the correct reading when the hog is somebody else. When the hog is us, the
starved sections come back looking like they were never asked for, and a context
silently missing evidence is exactly what this whole layer exists to prevent.

So the builder caps its own concurrency well below the shared pool
(``AIOPS_CONTEXT_WORKERS``, default 4), leaving slots free for the seams a collector
transitively calls. Any section that still reports starvation gets a note naming
``AIOPS_RESILIENCE_WORKERS`` as the lever, so the next person tunes the right knob.

Never raises
------------
``build`` degrades to a context whose sections are ``FAILED`` or ``UNAVAILABLE``. It
matches every comparable seam in this repo — ``resilience.guard``,
``topology.resolve``, ``collect_change_context``, ``retrieve_similar``,
``rca_agent.evidence.gather`` — all of which document the same contract, because a
failure on the incident path must cost evidence rather than a verdict. The single
exception is a denylisted capability, which is a programming error and raises at
request construction; see ``denylist.py``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from aiops.context import collectors as _collectors
from aiops.context import correlator, enricher, normalizer, ranker, redactor, tokenizer
from aiops.context.correlation import derive_correlation_id
from aiops.context.models import SectionSpec, SectionStatus, Source
from aiops.context.pack import (
    ContextSection,
    IncidentContext,
    IncidentIdentity,
    SourceProvenance,
)

logger = logging.getLogger(__name__)

_MAX_WORKERS_CEILING = 4
"""Upper bound on concurrent collectors, chosen against ``resilience``'s pool of 8.

Half the shared pool, so a full fan-out here can never starve the seams a collector
transitively calls. Raising this without raising ``AIOPS_RESILIENCE_WORKERS`` trades
a little latency for silently missing sections — see the module docstring.
"""


def _max_workers(requested: int) -> int:
    raw = os.environ.get("AIOPS_CONTEXT_WORKERS", "")
    try:
        ceiling = int(raw) if raw else _MAX_WORKERS_CEILING
    except ValueError:
        ceiling = _MAX_WORKERS_CEILING
    return max(1, min(ceiling if ceiling > 0 else _MAX_WORKERS_CEILING, max(requested, 1)))


class ContextRequest:
    """What a caller wants collected, and for which incident.

    A plain object rather than a Pydantic model: it is intra-process plumbing that
    never crosses a JSON boundary, which is the line
    ``aiops/tools/topology/graph.py`` draws between dataclass-style internals and
    validated boundary models. The *output* (``IncidentContext``) is the boundary
    model, and it is frozen and validated.

    ``specs`` is the caller's own list of queries — including its own PromQL. The
    platform owns the round-trip, the retry, the cache and the failure mapping; the
    agent owns the query. Five call sites use five different PromQL dialects that
    measure different things, so rewriting one here would change that agent's
    numbers with nothing in CI to catch it.
    """

    def __init__(
        self,
        *,
        service: str,
        window_start: datetime,
        window_end: datetime,
        specs: Sequence[SectionSpec],
        severity: str = "unknown",
        alert_id: str | None = None,
        alert_name: str | None = None,
        correlation_id: str | None = None,
        offline: bool = False,
    ) -> None:
        self.service = service
        self.window_start = window_start
        self.window_end = window_end
        self.specs = tuple(specs)
        self.severity = severity
        self.alert_id = alert_id
        self.alert_name = alert_name
        self.offline = offline
        # Derived when not supplied, so a standalone agent invocation lands on the
        # same id — and therefore the same cache entries — as an orchestrated run of
        # the same incident, with no coordination between them.
        self.correlation_id = correlation_id or derive_correlation_id(
            service, window_start, window_end
        )

    @property
    def requested_sources(self) -> frozenset[str]:
        return frozenset(spec.source for spec in self.specs)


def _skipped(reason: str) -> ContextSection:
    """A section nobody asked for."""
    return ContextSection(
        status=SectionStatus.NOT_REQUESTED,
        provenance=SourceProvenance(
            provider="none",
            status=SectionStatus.NOT_REQUESTED,
            coverage_note=reason,
        ),
    )


def _merge(existing: ContextSection | None, incoming: ContextSection) -> ContextSection:
    """Fold a second query's result for the same source into one section.

    One section can hold several queries — RCA alone issues about ten distinct PromQL
    queries against ``metrics``. The merge rule is deliberately asymmetric: the
    strongest status wins, but every payload is kept.

    Status precedence is ``COLLECTED > EMPTY > FAILED > UNAVAILABLE >
    NOT_REQUESTED``, so one query returning rows makes the section ``COLLECTED`` even
    if a sibling query failed. That is the honest reading — the section *does* carry
    evidence — and the failure is not lost: it stays in the provenance note, which is
    where a consumer looks to find out the section is partial.
    """
    if existing is None:
        return incoming

    order = {
        SectionStatus.COLLECTED: 4,
        SectionStatus.EMPTY: 3,
        SectionStatus.FAILED: 2,
        SectionStatus.UNAVAILABLE: 1,
        SectionStatus.NOT_REQUESTED: 0,
    }
    best = existing if order[existing.status] >= order[incoming.status] else incoming
    other = incoming if best is existing else existing

    raw: dict[str, object] | None = None
    if existing.raw or incoming.raw:
        raw = {**(existing.raw or {}), **(incoming.raw or {})}

    notes = [
        note for note in (best.provenance.coverage_note, other.provenance.coverage_note) if note
    ]
    return best.model_copy(
        update={
            "raw": raw,
            "provenance": best.provenance.model_copy(
                update={
                    "coverage_note": "; ".join(dict.fromkeys(notes)) or None,
                    "latency_ms": existing.provenance.latency_ms + incoming.provenance.latency_ms,
                    "error": best.provenance.error or other.provenance.error,
                }
            ),
        }
    )


class ContextBuilder:
    """Runs the pipeline. Stateless — safe to construct per call or share."""

    def build(
        self,
        request: ContextRequest,
        *,
        now: datetime | None = None,
        profile: str | None = None,
        max_tokens: int | None = None,
    ) -> IncidentContext:
        """Collect, process and freeze one incident's context.

        ``now`` is injected rather than read from the clock so stages 4 and 5 stay
        reproducible: the ranker decays by age, and a test or an eval that could not
        pin "now" could not assert a score. It defaults to the wall clock for
        production callers.

        Budgeting (stage 7) runs only when ``profile`` and ``max_tokens`` are both
        given. A context nobody has budgeted carries ``token_budget=None``, which is
        the honest statement that it has not been trimmed for anyone — as opposed to
        having been trimmed with a limit that happened not to bite.
        """
        moment = now or datetime.now(UTC)
        sections = self._collect(request)

        # Stages 2-6 are pure. Ordering between them is not arbitrary: correlation
        # needs normalized signatures to detect cross-source agreement, ranking reads
        # the metadata correlation writes, enrichment must not overwrite it, and
        # redaction runs LAST so nothing it scrubs can have been copied into metadata
        # by an earlier stage.
        sections = normalizer.normalize(
            sections,
            correlation_id=request.correlation_id,
            incident_service=request.service,
            fallback_timestamp=request.window_end,
        )
        sections = correlator.correlate(
            sections,
            incident_service=request.service,
            dependencies=self._dependencies(sections),
        )
        sections = enricher.enrich(sections, incident_service=request.service)
        sections, security = redactor.redact(sections)

        observations = tuple(obs for section in sections.values() for obs in section.observations)
        ranking = ranker.rank(observations, now=moment, incident_service=request.service)

        pack = IncidentContext(
            incident=IncidentIdentity(
                service=request.service,
                severity=request.severity,
                window_start=request.window_start,
                window_end=request.window_end,
                correlation_id=request.correlation_id,
                alert_id=request.alert_id,
                alert_name=request.alert_name,
            ),
            built_at=moment,
            evidence_ranking=ranking,
            security=security,
            # Named explicitly rather than splatted from ``sections``. A ``**dict``
            # here type-checks as "every keyword might be a ContextSection", which
            # collides with ``schema_version: int`` and ``token_budget`` and defeats
            # mypy on the one construction site where a missing or misnamed section
            # would matter. Verbose, but a renamed section becomes a type error
            # instead of a runtime ``ValidationError`` on the incident path.
            metrics=sections["metrics"],
            logs=sections["logs"],
            traces=sections["traces"],
            k8s_events=sections["k8s_events"],
            topology=sections["topology"],
            dependencies=sections["dependencies"],
            deployments=sections["deployments"],
            incident_history=sections["incident_history"],
            oncall=sections["oncall"],
            cmdb=sections["cmdb"],
            runbooks=sections["runbooks"],
        )

        if profile is not None and max_tokens is not None:
            pack = tokenizer.budget(pack, profile=profile, max_tokens=max_tokens)
        return pack

    # --- stage 1 ---------------------------------------------------------

    def _collect(self, request: ContextRequest) -> dict[str, ContextSection]:
        """Fan out over the requested specs and fold the results into sections.

        Every source is represented in the returned dict, including the ones nobody
        asked for — ``IncidentContext`` requires all eleven, and a caller must be able
        to tell "not requested" from "requested and unavailable" without checking
        whether a key exists.
        """
        every_source: tuple[str, ...] = tuple(Source.__args__)  # type: ignore[attr-defined]
        sections: dict[str, ContextSection] = {}

        if request.offline:
            # The zero-I/O path. The eval harness runs agents with synthetic evidence
            # so goldens are reproducible without a backend; this makes the same
            # guarantee available to a caller of the context layer, and lets a test
            # assert that a golden run made no guarded call at all.
            return {name: _skipped("offline build requested") for name in every_source}

        known, unknown = _collectors.resolve_chain()
        eligible = set(known)
        if unknown:
            # Returned rather than logged and dropped, following
            # ``change_context/collector.py``: a chain of nothing but typos would
            # otherwise produce a context that had asked no one and looked complete.
            logger.warning("unknown context collectors ignored: %s", ", ".join(unknown))

        runnable = [spec for spec in request.specs if spec.source in eligible]
        skipped_specs = [spec for spec in request.specs if spec.source not in eligible]

        results: list[tuple[SectionSpec, ContextSection]] = []
        if runnable:
            with ThreadPoolExecutor(
                max_workers=_max_workers(len(runnable)),
                thread_name_prefix="aiops-context",
            ) as pool:
                results = list(
                    zip(
                        runnable,
                        pool.map(lambda spec: self._collect_one(spec, request), runnable),
                        strict=True,
                    )
                )

        for spec, section in results:
            sections[spec.source] = _merge(sections.get(spec.source), section)

        for spec in skipped_specs:
            sections.setdefault(
                spec.source,
                _skipped(f"collector {spec.source!r} excluded by AIOPS_CONTEXT_COLLECTORS"),
            )

        for name in every_source:
            sections.setdefault(name, _skipped("not requested"))
        return sections

    def _collect_one(self, spec: SectionSpec, request: ContextRequest) -> ContextSection:
        """Run one collector. Never raises — a collector bug costs its own section."""
        collector = _collectors.collector_for(spec.source)
        if collector is None:
            return _collectors.unavailable(spec.source, "none", "no collector for this source")
        try:
            return collector.collect(spec, request.correlation_id)
        except Exception as exc:
            # Collectors document that they do not raise, and each is defensive.
            # This exists because ``build`` must not raise either, so a broken
            # collector has to cost one section rather than the whole context.
            logger.debug("context collector %s raised", spec.source, exc_info=True)
            return ContextSection(
                status=SectionStatus.FAILED,
                provenance=SourceProvenance(
                    provider=getattr(collector, "name", spec.source),
                    status=SectionStatus.FAILED,
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )

    @staticmethod
    def _dependencies(sections: dict[str, ContextSection]) -> tuple[str, ...]:
        """The incident service's direct dependencies, for the correlator.

        Read from whichever of the two structural sections answered. ``topology`` is
        preferred over ``dependencies`` because it is the resolver *chain* — it tried
        OTel, then the CMDB, then the static table — whereas ``dependencies`` is the
        single ``itsm.cmdb.dependencies`` capability. Falling back rather than
        merging keeps the provenance honest: the correlator's topology relations come
        from one named source, not a union of sources that may disagree.
        """
        for name in ("topology", "dependencies"):
            section = sections.get(name)
            if section is None or not section.status.usable or not section.raw:
                continue
            for payload in section.raw.values():
                if isinstance(payload, dict):
                    deps = payload.get("dependencies")
                    if isinstance(deps, list | tuple) and deps:
                        return tuple(str(dep) for dep in deps)
        return ()


_BUILDER = ContextBuilder()


def build(
    request: ContextRequest,
    *,
    now: datetime | None = None,
    profile: str | None = None,
    max_tokens: int | None = None,
) -> IncidentContext:
    """Module-level entry point, mirroring ``resolve``/``search_similar``/
    ``collect_change_context`` so callers reach a seam through a function rather than
    having to know it is implemented as a class."""
    return _BUILDER.build(request, now=now, profile=profile, max_tokens=max_tokens)
