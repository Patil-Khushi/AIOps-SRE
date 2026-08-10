"""Historical incident retrieval for RA-007 — evidence only.

Attaches "we have seen something like this before" to a correlation result, and
nothing more. It does not name a cause for the current incident, does not rank
hypotheses, and does not recommend an action: those are the RCA agent's job, and
performing them here would launder inference into what is supposed to be
retrieval.

The line that keeps it honest: a past incident's recorded cause is a historical
fact and therefore evidence. Asserting the current incident shares that cause is
inference. ``SimilarIncidents`` carries the former and has no field for the latter.

Opt-in via ``AIOPS_INCIDENT_HISTORY`` (default off) so ``correlate()`` acquires no
new dependency on the incident path, and the eval harness stays hermetic.
"""

from __future__ import annotations

import logging
import os

from pydantic import BaseModel, ConfigDict, Field

from aiops.tools.incident_history import (
    IncidentMatch,
    RetrievalQuery,
    RetrievalStatus,
    search_similar,
)

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("AIOPS_INCIDENT_HISTORY", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}
_LIMIT = int(os.environ.get("AIOPS_INCIDENT_HISTORY_LIMIT", "5"))
_MIN_SIMILARITY = float(os.environ.get("AIOPS_INCIDENT_HISTORY_MIN_SIMILARITY", "0.1"))


class SimilarIncidents(BaseModel):
    """Past incidents resembling this one, with retrieval provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matches: list[IncidentMatch] = Field(default_factory=list)
    provider: str | None = None
    """Which backend answered. Material to how much the evidence is worth: a
    semantic match from a populated vector store and a keyword match from a
    15-row demo corpus are not equivalent."""

    providers_attempted: list[str] = Field(default_factory=list)
    corpus_size: int | None = None
    """Population searched. A similarity score is uninterpretable without it —
    "no matches" across 15 incidents means little; across 10,000 it means a lot."""

    coverage_note: str | None = None
    """Why the result is incomplete, when it is. Non-``None`` whenever retrieval
    was disabled, unconfigured or failed, so an empty match list is never read as
    "this has never happened before"."""

    @property
    def best(self) -> IncidentMatch | None:
        """Highest-scoring match, or ``None``.

        A convenience for display. Explicitly *not* a conclusion — the caller
        still decides whether a match is relevant.
        """
        return self.matches[0] if self.matches else None


def retrieve_similar(
    service: str,
    signatures: list[str],
    topology: list[str],
) -> SimilarIncidents | None:
    """Retrieve similar past incidents, or ``None`` when disabled.

    ``None`` means "not attempted", which is deliberately distinct from an empty
    match list meaning "searched, found nothing". Never raises: history is an
    enrichment and must not cost a verdict.
    """
    if not _ENABLED:
        return None

    query = RetrievalQuery(
        service=service,
        signatures=signatures,
        services_involved=[service, *topology],
        topology=topology,
        limit=_LIMIT,
        min_similarity=_MIN_SIMILARITY,
    )

    try:
        attempts = search_similar(query)
    except Exception as exc:
        logger.warning("incident history retrieval failed: %s", exc)
        return SimilarIncidents(
            coverage_note=f"retrieval raised {type(exc).__name__}; no history available"
        )

    winner = next((a for a in attempts if a.matched), None)
    attempted = [a.provider for a in attempts]

    if winner is not None:
        return SimilarIncidents(
            matches=list(winner.matches),
            provider=winner.provider,
            providers_attempted=attempted,
            corpus_size=winner.corpus_size,
        )

    # Nothing matched. Distinguish "searched and found nothing" from "could not
    # search" — the first says this incident looks novel, the second says nothing
    # at all, and treating them alike would be the more dangerous error.
    searched = [a for a in attempts if a.status is RetrievalStatus.EMPTY]
    if searched:
        return SimilarIncidents(
            provider=searched[0].provider,
            providers_attempted=attempted,
            corpus_size=searched[0].corpus_size,
            coverage_note=searched[0].note or "no similar incident found in the corpus",
        )
    notes = "; ".join(f"{a.provider}: {a.note or a.error or a.status.value}" for a in attempts)
    return SimilarIncidents(
        providers_attempted=attempted,
        coverage_note=f"no backend could be searched ({notes or 'no providers configured'})",
    )
