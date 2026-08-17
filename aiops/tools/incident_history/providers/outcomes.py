"""Incident history sourced from *verified RCA outcomes* — the only memory RCA may use.

Why this provider exists when ``mock`` and ``embedding`` already answer the same
question
--------------------------------------------------------------------------------
Both of those search :mod:`aiops.tools.incident_history.corpus`, which loads the
repository's truth files and maps each file's ``root_cause`` onto
``ResolutionMetadata.recorded_cause``. For most consumers that is exactly right —
the truth files are genuine recorded incidents with genuine resolutions, and a
demo of historical retrieval should use real history rather than invented history.

For the RCA agent it is the answer key. Every ecommerce truth file is also a graded
evaluation case, so a recall over that corpus while investigating one of those
scenarios hands the agent the very field the evaluation is about to grade it on.
The resulting accuracy would measure lookup, not diagnosis.

This provider searches a different population: ``rca_outcomes`` rows, which record
what happened to a *past prediction* — what was proposed, whether a human approved
it, what was executed, and whether the resolution verifier confirmed recovery. A row
only becomes recallable once that verification happened, so the corpus is grounded in
observed recoveries rather than in a document that already knows the answer.

Only ``verified`` and ``trusted`` rows are ever returned. That filter lives here as
well as in the agent's eligibility check, deliberately: the store is the last place
that can guarantee an unverified prediction never leaves it, and a defence that
depends on every caller remembering to filter is not a defence.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from aiops.tools.incident_history.base import (
    IncidentHistoryProvider,
    IncidentMatch,
    ResolutionMetadata,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStatus,
    jaccard,
    overlap,
    token_jaccard,
)

logger = logging.getLogger(__name__)

# Same weighting rationale as the mock provider, minus its topology term: an outcome
# row records the service it happened to and the symptoms that were observed, and the
# dependency shape at the time is not part of the record. Reweighted rather than
# carried at zero, so the score still spans 0-1 and a strong match reads as strong.
_W_SIGNATURES = 0.55
"""Exact symptom-identifier overlap. Dominant because it is the only dimension that
reflects *what went wrong* — the same alert and metric names firing again is the
substance of a recurrence."""

_W_TOKENS = 0.2
"""Token-level overlap, as a weaker second dimension. Recovers matches between
differently-worded descriptions of one event without letting shared vocabulary
masquerade as a verbatim hit."""

_W_SERVICES = 0.25
"""Same-service agreement. Refines the score; never drives it. A recurrence on one
service is a lead, but an identical signature on a *different* service is often the
more useful match, so this cannot dominate."""


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class RcaOutcomeHistoryProvider(IncidentHistoryProvider):
    """Recall past incidents from verified RCA outcomes held in ``aiops.state``.

    In-process (SQLite by default) and therefore fast and always reachable, but it is
    still reported ``UNAVAILABLE`` rather than ``EMPTY`` when the store cannot be read
    — "could not ask" and "asked, nothing similar" are different answers, and an
    unreadable database must not look like a clean history.
    """

    name = "rca_outcomes"

    def health(self) -> tuple[bool, str]:
        try:
            from aiops.state.repository import RECALLABLE_MEMORY_STATUSES, count_rca_outcomes

            total = count_rca_outcomes()
            recallable = count_rca_outcomes(statuses=RECALLABLE_MEMORY_STATUSES)
        except Exception as exc:
            return False, f"outcome store unreadable: {type(exc).__name__}: {exc}"
        # Healthy with zero rows: an empty store is a cold start, which is a valid
        # state to be in and not a malfunction. Reporting it unhealthy would make a
        # brand-new deployment indistinguishable from a broken one.
        return True, f"{recallable} recallable of {total} recorded outcome(s)"

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.perf_counter()
        try:
            from aiops.state.repository import RECALLABLE_MEMORY_STATUSES, list_rca_outcomes

            rows = list_rca_outcomes(
                statuses=RECALLABLE_MEMORY_STATUSES,
                # Not filtered by service: an identical signature on a neighbouring
                # service is frequently the more informative precedent, and the
                # service term below already rewards same-service agreement. Filtering
                # here would hide exactly the cross-service recurrences worth seeing.
                limit=200,
            )
            corpus_size = len(rows)
        except Exception as exc:
            logger.debug("rca_outcomes: store unreadable (%s)", exc)
            return RetrievalResult(
                provider=self.name,
                status=RetrievalStatus.UNAVAILABLE,
                error=f"{type(exc).__name__}: {exc}",
                note="outcome store could not be read; this is not an empty history",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        matches: list[IncidentMatch] = []
        for row in rows:
            signatures = [str(s) for s in row.get("signatures") or []]
            services = [str(row.get("affected_service") or "")]

            sig_exact = jaccard(query.signatures, signatures)
            sig_tokens = token_jaccard(query.signatures, signatures)
            svc = jaccard(query.services_involved or [query.service], services)

            # No shared symptom means no recurrence, whatever the service agreement says.
            # Without this the service term alone (0.25) clears the similarity floor, so
            # *every* past incident on a service became a prior for *every* new incident
            # on it — "this service has had incidents before", dressed up as precedent.
            if sig_exact <= 0.0 and sig_tokens <= 0.0:
                continue

            score = round(
                _W_SIGNATURES * sig_exact + _W_TOKENS * sig_tokens + _W_SERVICES * svc,
                4,
            )
            if score < query.min_similarity:
                continue

            corrected = row.get("human_corrected_root_cause")
            recorded_cause = corrected or row.get("predicted_root_cause") or None
            matches.append(
                IncidentMatch(
                    incident_id=str(row.get("incident_id") or ""),
                    similarity_score=min(1.0, score),
                    title=str(row.get("predicted_root_cause") or "")[:120] or None,
                    occurred_at=_parse_dt(row.get("recorded_at")),
                    matching_signatures=overlap(query.signatures, signatures),
                    matching_services=overlap(query.services_involved or [query.service], services),
                    resolution=ResolutionMetadata(
                        # Only verified/trusted rows reach here, and verification
                        # means the verifier confirmed recovery.
                        resolved=True,
                        # The human correction wins when one exists. A correction is
                        # the one path by which a *failed* prediction still teaches
                        # something, and remembering the prediction over the
                        # correction would teach the mistake instead.
                        recorded_cause=recorded_cause,
                        resolution_summary=row.get("action_key") or None,
                        resolved_at=_parse_dt(row.get("recorded_at")),
                        recorded_hypothesis_class=row.get("selected_hypothesis_class"),
                        runbook_ref=row.get("action_key") or None,
                    ),
                    provider=self.name,
                    match_explanation=(
                        f"signatures {sig_exact:.2f} exact / {sig_tokens:.2f} token, "
                        f"service {svc:.2f}"
                        + (" (cause is a human correction)" if corrected else "")
                    ),
                )
            )

        matches.sort(key=lambda m: (-m.similarity_score, m.incident_id))
        limited = matches[: max(1, query.limit)]
        return RetrievalResult(
            provider=self.name,
            status=RetrievalStatus.MATCHED if limited else RetrievalStatus.EMPTY,
            matches=limited,
            corpus_size=corpus_size,
            note=(
                None
                if limited
                else f"no verified outcome scored above {query.min_similarity} "
                f"across {corpus_size} recallable outcome(s)"
            ),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )


__all__ = ["RcaOutcomeHistoryProvider"]
