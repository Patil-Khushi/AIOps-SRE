"""Read-only Historical Incident RAG — semantic search over PAST, PERSISTED
RCA verdicts, for the chat's "has this happened before?" style questions.

A different corpus and a different purpose from
``agents/rca_agent/investigation/memory.py::recall()`` (RCA's
confidence-affecting prior recall, restricted to
``OUTCOME_BACKED_PROVIDERS``): this module

* searches ``aiops.state.repository``'s persisted RCA verdicts — real
  incidents THIS deployment has actually processed, not the truth-file eval
  corpus and not the current in-flight incident;
* is never imported by ``analyze()``, ``investigation/memory.py``, or
  anything on the scoring path;
* never writes anything, never promotes memory, never changes a trust state;
* returns retrieval scores and historical facts only — no field here claims
  the past cause applies now (mirrors
  ``aiops/tools/incident_history/base.py``'s own design principle: "a past
  incident's recorded cause is a historical fact; asserting the current
  incident has the same cause is inference, and nothing here does it").

Boundary, checked by AST in ``tests/test_rca_chat_boundary.py``: this module
never references the policy gate, the tool registry, ``analyze()``, or the
investigation memory subsystem.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from aiops.state import repository as state_repo

logger = logging.getLogger(__name__)

# Cosine on short incident-summary sentences clusters around 0.1-0.3 from
# shared "incident report" vocabulary alone (the same observation that sets
# aiops/tools/incident_history/providers/embedding.py's _SEMANTIC_FLOOR at
# 0.35) — set higher here because a chat answer citing a specific past
# incident by id is a stronger claim than a correlation-scoring signal, and
# a marginal match presented as "similar" is worse than none.
DEFAULT_MIN_SIMILARITY = float(os.environ.get("AIOPS_RCA_RAG_MIN_SIMILARITY", "0.55"))
DEFAULT_LIMIT = int(os.environ.get("AIOPS_RCA_RAG_LIMIT", "3"))

# Only a settled cause is usable precedent — an UNCERTAIN or
# INSUFFICIENT_EVIDENCE past incident has nothing reliable to compare against.
_ELIGIBLE_STATUSES = frozenset({"confirmed", "probable"})


@dataclass(frozen=True)
class SimilarIncident:
    """One past, RESOLVED incident judged similar — a historical fact, never
    a claim about the current incident. Deliberately has no "applies here"
    field, matching ``ResolutionMetadata``'s design in
    ``aiops/tools/incident_history/base.py``."""

    incident_id: str
    similarity: float
    affected_service: str
    root_cause_summary: str
    category: str | None
    recorded_fix: str | None
    occurred_at: str | None


def _signature_text(*, service: str, summary: str, category: str | None) -> str:
    """The text an incident is embedded/queried by. Cause/category first,
    service last — mirrors the embedding provider's "failure mode in prose
    first" ordering (aiops/tools/incident_history/providers/embedding.py
    ::_document_text)."""
    parts = [summary, category or "", service]
    return ". ".join(p for p in parts if p and p.strip())


def _category_of(verdict: dict) -> str | None:
    inv = verdict.get("investigation") or {}
    matrices = inv.get("matrices") or []
    if not matrices:
        return None
    selected_id = inv.get("selected_hypothesis_id")
    for m in matrices:
        hyp = m.get("hypothesis") or {}
        if selected_id is not None and hyp.get("hypothesis_id") == selected_id:
            return hyp.get("category")
    return (matrices[0].get("hypothesis") or {}).get("category")


def _recorded_fix(verdict: dict) -> str | None:
    """The fix RECORDED for that past incident. Prefers the recommended
    remediation option (bolted onto the persisted verdict by
    ``demo/ui/server.py`` before it was saved, alongside
    ``recommended_option_id``); falls back to the top ranked fix step. Named
    "recorded", not "recommended for you" — the caller is responsible for
    phrasing it as history (see ``chat.py``'s prompt clause)."""
    options = verdict.get("remediation_options") or []
    recommended_id = verdict.get("recommended_option_id")
    if options:
        chosen = next((o for o in options if o.get("option_id") == recommended_id), options[0])
        desc = chosen.get("description")
        if desc:
            return str(desc)
    steps = verdict.get("ranked_fix_steps") or []
    if steps:
        desc = steps[0].get("description")
        if desc:
            return str(desc)
    return None


def _eligible_rows(exclude_incident_id: str | None) -> list[dict]:
    rows = state_repo.list_rca_results(limit=200, exclude_incident_id=exclude_incident_id)
    eligible = []
    for row in rows:
        v = row.get("verdict") or {}
        status = str(v.get("root_cause_status") or "").lower()
        if status in _ELIGIBLE_STATUSES:
            eligible.append(row)
    return eligible


def search_similar_incidents(
    *,
    service: str,
    summary: str,
    category: str | None = None,
    exclude_incident_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> list[SimilarIncident]:
    """Semantic search over persisted, RESOLVED RCA verdicts.

    Returns only matches at or above ``min_similarity`` — never a blind
    top-K. An empty list (never an exception, never a guess) means either
    "the embedding model is unavailable" or "nothing in the eligible corpus
    reached the floor" — both render as the same honest chat answer ("no
    sufficiently similar resolved incident was found"), which is correct:
    neither case is evidence that this incident is unprecedented, only that
    this search could not establish precedent.
    """
    try:
        from aiops.tools.incident_history.providers.embedding import get_shared_model
    except Exception as exc:
        logger.info("incident RAG: embedding module unavailable (%s)", exc)
        return []

    model = get_shared_model()
    if model is None:
        return []

    rows = _eligible_rows(exclude_incident_id)
    if not rows:
        return []

    query_text = _signature_text(service=service, summary=summary, category=category)
    if not query_text.strip():
        return []

    documents = [
        _signature_text(
            service=row.get("affected_service", ""),
            summary=str((row.get("verdict") or {}).get("root_cause") or ""),
            category=_category_of(row.get("verdict") or {}),
        )
        for row in rows
    ]

    try:
        vectors = model.encode(
            [*documents, query_text], normalize_embeddings=True, show_progress_bar=False
        )
    except Exception as exc:
        logger.warning("incident RAG: embedding encode failed (%s)", exc)
        return []

    qvec = vectors[-1]
    doc_vecs = vectors[:-1]

    scored: list[tuple[float, dict]] = []
    for row, vec in zip(rows, doc_vecs, strict=True):
        score = float(sum(a * b for a, b in zip(qvec, vec, strict=True)))
        score = max(
            0.0, min(1.0, score)
        )  # both sides L2-normalised: dot == cosine, clamp guards float noise
        if score >= min_similarity:
            scored.append((score, row))

    # Highest similarity first; incident_id as a deterministic tiebreaker.
    scored.sort(key=lambda t: (-t[0], t[1]["incident_id"]))
    top = scored[: max(1, limit)]

    results: list[SimilarIncident] = []
    for score, row in top:
        v = row.get("verdict") or {}
        results.append(
            SimilarIncident(
                incident_id=row["incident_id"],
                similarity=round(score, 3),
                affected_service=row.get("affected_service", ""),
                root_cause_summary=str(v.get("root_cause") or ""),
                category=_category_of(v),
                recorded_fix=_recorded_fix(v),
                occurred_at=row.get("created_at"),
            )
        )
    return results
