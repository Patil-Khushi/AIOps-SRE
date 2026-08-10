"""Static incident-history provider, sourced from the demo truth files.

``demo/truth_files/*.yaml`` already record, per scenario, what actually broke and
what the correct fix was — written specifically so the eval harness has ground
truth. That makes them the honest corpus for a historical-retrieval demo: real
recorded incidents with real resolutions, rather than fabricated history invented
to make retrieval look good.

Terminal tier of the chain, always available, no I/O beyond one file read at
import. It is what lets retrieval degrade to *something defensible* when no vector
store or database is configured.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

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

_TRUTH_DIR = Path(
    os.environ.get(
        "AIOPS_TRUTH_FILES_DIR",
        str(Path(__file__).resolve().parents[4] / "demo" / "truth_files"),
    )
)

# Weights for the composite similarity score. Signature overlap dominates because
# it is the only dimension that reflects *what went wrong*: two incidents on the
# same service with unrelated errors are a weak match, while the same error
# signature on a different service is a strong lead. Services and topology refine
# the score rather than drive it.
_W_SIGNATURES = 0.45
_W_TOKENS = 0.2
_W_SERVICES = 0.2
_W_TOPOLOGY = 0.15

# Exact signature match stays the dominant term; token overlap is a deliberately
# weaker second dimension. Observed here: the agent emits "Payment charge failed:
# payment service unavailable" while the recorded incident says
# "PaymentErrorRateHigh alert firing" — the same event with no shared string, so
# exact-only scoring returned nothing at all. Token overlap recovers that match
# without letting shared vocabulary masquerade as a verbatim hit.


def _first_fix_description(data: dict) -> str | None:
    """The first recorded fix step's description, or ``None``.

    ``None`` rather than a substitute: if a truth file records no fix steps, then
    what resolved that incident is genuinely unknown, and the honest answer to
    "how was this resolved?" is silence. Falling back to a cause summary or a
    hypothesis would manufacture a resolution that nobody performed.
    """
    steps = data.get("expected_fix_steps") or []
    if not isinstance(steps, list) or not steps:
        return None
    first = steps[0]
    if not isinstance(first, dict):
        return str(first) or None
    description = str(first.get("description") or "").strip()
    return description or None


def _load_corpus() -> list[dict]:
    """Parse the truth files into plain incident records.

    Tolerant by design: a malformed or missing truth file degrades the corpus
    rather than breaking retrieval, because history is an enrichment and must
    never cost a correlation.
    """
    corpus: list[dict] = []
    if not _TRUTH_DIR.is_dir():
        logger.debug("incident_history: truth dir %s not found", _TRUTH_DIR)
        return corpus

    try:
        import yaml
    except Exception:  # pragma: no cover - yaml ships with the repo's deps
        logger.debug("incident_history: pyyaml unavailable")
        return corpus

    for path in sorted(_TRUTH_DIR.glob("*.yaml")):
        if path.stem == "template":
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.debug("incident_history: skipping %s (%s)", path.name, exc)
            continue
        if not isinstance(data, dict):
            continue

        cause = data.get("real_cause") or {}
        rca = data.get("expected_rca") or {}
        component = str(cause.get("component") or "").strip().lower()
        signals = [str(s) for s in (rca.get("evidence_signals") or []) if s]

        corpus.append(
            {
                "incident_id": str(data.get("scenario_id") or path.stem),
                "title": data.get("title"),
                "occurred_at": data.get("last_updated"),
                "signatures": signals,
                "services": [component] if component else [],
                "topology": [component] if component else [],
                "recorded_cause": cause.get("description") or rca.get("cause_summary"),
                # The first *fix step*, not the first ranked hypothesis. A
                # hypothesis is a candidate cause someone proposed; a resolution is
                # what was actually done. Sourcing this field from
                # ``ranked_hypotheses[0]`` relabelled a guess as settled fact, and
                # since this corpus is retrieval evidence handed to the RCA agent,
                # that turns one incident's speculation into another's precedent.
                "resolution_summary": _first_fix_description(data),
                "owner": data.get("owner"),
            }
        )
    return corpus


_CORPUS: list[dict] | None = None


def _corpus() -> list[dict]:
    """Lazily parsed and cached: the files do not change during a process, and
    parsing on import would make ``import aiops.tools`` do disk I/O."""
    global _CORPUS
    if _CORPUS is None:
        _CORPUS = _load_corpus()
    return _CORPUS


def reset_corpus_for_tests() -> None:
    """Test seam — the cache is module state, so a test pointing at a different
    truth dir would otherwise get the previous corpus."""
    global _CORPUS
    _CORPUS = None


def _parse_date(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class MockIncidentHistoryProvider(IncidentHistoryProvider):
    """Similarity search over the demo truth files."""

    name = "mock"

    def health(self) -> tuple[bool, str]:
        size = len(_corpus())
        # Reported healthy even when empty: an empty corpus is a real state that
        # should surface as EMPTY from a search, not as an unhealthy provider.
        return True, f"{size} historical incident(s) in corpus"

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.monotonic()
        corpus = _corpus()

        matches: list[IncidentMatch] = []
        for record in corpus:
            sig_score = jaccard(query.signatures, record["signatures"])
            tok_score = token_jaccard(query.signatures, record["signatures"])
            # An exact hit on the *incident* service is a full service-level
            # match, not a fraction of it. Folding it into one Jaccard with the
            # topology makes the score fall as the topology grows: with five
            # dependencies, a past incident on this very service scored 1/6 and
            # ranked *below* unrelated incidents that happened to share a word.
            # Breadth of context must not dilute an exact identity match.
            svc_score = jaccard([query.service, *query.services_involved], record["services"])
            if query.service and query.service in record["services"]:
                svc_score = 1.0
            topo_score = jaccard(query.topology, record["topology"])
            score = round(
                sig_score * _W_SIGNATURES
                + tok_score * _W_TOKENS
                + svc_score * _W_SERVICES
                + topo_score * _W_TOPOLOGY,
                4,
            )
            if score < query.min_similarity:
                continue

            matches.append(
                IncidentMatch(
                    incident_id=record["incident_id"],
                    similarity_score=min(score, 1.0),
                    title=record.get("title"),
                    occurred_at=_parse_date(record.get("occurred_at")),
                    matching_signatures=overlap(query.signatures, record["signatures"]),
                    matching_services=overlap(
                        [query.service, *query.services_involved], record["services"]
                    ),
                    matching_topology=overlap(query.topology, record["topology"]),
                    resolution=ResolutionMetadata(
                        resolved=True,
                        resolution_summary=record.get("resolution_summary"),
                        recorded_cause=record.get("recorded_cause"),
                        resolved_by=record.get("owner"),
                    ),
                    provider=self.name,
                    match_explanation=(
                        f"signatures={sig_score} (w{_W_SIGNATURES}), "
                        f"tokens={tok_score} (w{_W_TOKENS}), "
                        f"services={svc_score} (w{_W_SERVICES}), "
                        f"topology={topo_score} (w{_W_TOPOLOGY})"
                    ),
                )
            )

        # Highest first, then by id so equal scores order deterministically —
        # otherwise the same query renders differently between runs.
        matches.sort(key=lambda m: (-m.similarity_score, m.incident_id))
        matches = matches[: query.limit]

        latency = (time.monotonic() - started) * 1000.0
        if matches:
            return RetrievalResult(
                provider=self.name,
                status=RetrievalStatus.MATCHED,
                matches=matches,
                latency_ms=latency,
                corpus_size=len(corpus),
            )
        return RetrievalResult(
            provider=self.name,
            status=RetrievalStatus.EMPTY,
            latency_ms=latency,
            corpus_size=len(corpus),
            note=(
                f"no incident in a corpus of {len(corpus)} scored at or above "
                f"{query.min_similarity}"
            ),
        )
