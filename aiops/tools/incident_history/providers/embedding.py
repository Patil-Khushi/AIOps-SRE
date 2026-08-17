"""Semantic incident retrieval from an in-process embedding index.

This is the runnable counterpart to ``VectorIncidentHistoryProvider``, which is
deliberately a stub because no Qdrant/pgvector exists in this deployment and
writing query code against an unagreed remote schema would look finished without
being runnable. That reasoning does not apply here: the corpus is *our own* truth
files, so there is no unknown schema, and the whole index can be built in memory.

Why no vector database
----------------------
The corpus is 27 incidents. A vector server is the right answer at 10^5 documents
and pure overhead at 10^1 — it would add a deployment, a port-forward, a failure
mode and ~300MB of RAM on a 16GB laptop to accelerate a dot product over 27 rows
that takes microseconds. Exact cosine over the full corpus is not an
approximation of what a vector store would return: it is the same answer, without
the ANN index that only pays off when exhaustive search stops being feasible.

Vendor neutrality is preserved by construction (CLAUDE.md principle #1): this is
one implementation behind ``IncidentHistoryProvider``, selected by name in
``AIOPS_INCIDENT_HISTORY_PROVIDERS``. Swapping in Qdrant later means registering a
different provider, not touching a caller.

What it adds over the mock
--------------------------
The mock scores set overlap (Jaccard on signatures/services/topology), so it can
only match incidents that share literal tokens. Embeddings match *meaning*:
``ad_high_cpu`` ("Ad service CPU saturation", from the Astronomy Shop corpus) and
``user_service_high_cpu`` ("Application CPU saturation", from the ecommerce SUT)
describe the same failure mode with no shared service, alertname or keyword. Set
overlap scores that pair at zero; cosine does not. That gap is the entire reason
to run an embedding tier at all.

Honesty posture, unchanged from the rest of this seam: a model that will not load
is ``UNAVAILABLE`` (the corpus was never searched), while a loaded model that
found nothing above the floor is ``EMPTY`` (a real answer about a real corpus).
Collapsing those would let a broken embedding stack read as "this has never
happened before".
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from aiops.tools.incident_history.base import (
    IncidentHistoryProvider,
    IncidentMatch,
    ResolutionMetadata,
    RetrievalQuery,
    RetrievalResult,
    RetrievalStatus,
    overlap,
)
from aiops.tools.incident_history.corpus import load_corpus

logger = logging.getLogger(__name__)

_MODEL_NAME = os.environ.get(
    "AIOPS_INCIDENT_HISTORY_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
)

# Cosine and Jaccard are not on the same scale, so the chain-wide
# AIOPS_INCIDENT_HISTORY_MIN_SIMILARITY floor (0.1, tuned for set overlap) is far
# too permissive for embeddings: all-MiniLM puts two unrelated ops sentences
# around 0.1-0.3 purely because they share the register of English that incident
# reports are written in. A 0.1 floor would return the whole corpus as "similar".
# The effective floor is max(caller floor, this one) so a caller can still tighten
# it but cannot accidentally loosen it into noise.
_SEMANTIC_FLOOR = float(os.environ.get("AIOPS_INCIDENT_HISTORY_EMBED_FLOOR", "0.35"))

# None = not yet attempted, False = attempted and unavailable, else the model.
# Same tri-state as agents/alert_triage/agent.py::_get_embed_model, for the same
# reason: retrying a broken import on every correlation costs seconds and yields
# the same failure.
_MODEL: Any = None
_UNAVAILABLE_REASON: str = ""

# Corpus embeddings, computed once per process. Keyed by model name so changing
# AIOPS_INCIDENT_HISTORY_EMBED_MODEL in a test does not silently reuse vectors
# from a different model — comparing vectors across models is meaningless.
_INDEX: tuple[str, list[dict], Any] | None = None


def _get_model() -> Any | None:
    """Load the sentence-transformers model, or ``None`` with a recorded reason."""
    global _MODEL, _UNAVAILABLE_REASON
    if _MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer

            _MODEL = SentenceTransformer(_MODEL_NAME)
            logger.info("incident_history: loaded embedding model %s", _MODEL_NAME)
        except Exception as exc:
            # Covers ImportError (the `embeddings` extra is not installed) and
            # runtime loader failures — notably OSError [WinError 126] when a
            # torch native DLL cannot load because the MSVC++ runtime is absent,
            # which is a real and non-obvious failure on Windows.
            _UNAVAILABLE_REASON = f"{exc.__class__.__name__}: {exc}"
            logger.info(
                "incident_history: embedding model unavailable (%s); "
                "install with `uv sync --extra embeddings`",
                _UNAVAILABLE_REASON,
            )
            _MODEL = False
    return _MODEL if _MODEL else None


def _document_text(record: dict) -> str:
    """The text an incident is indexed by.

    Cause and title first because they carry the failure *mode* in prose, which is
    what cosine can generalise over. Signatures are appended for lexical anchoring
    but kept last so a long signature list cannot dominate the vector.
    """
    parts = [
        str(record.get("title") or ""),
        str(record.get("recorded_cause") or ""),
        " ".join(str(s) for s in record.get("services") or []),
        " ".join(str(s) for s in record.get("signatures") or []),
    ]
    return ". ".join(p for p in parts if p.strip())


def _query_text(query: RetrievalQuery) -> str:
    parts = [
        query.service,
        " ".join(query.signatures),
        " ".join(query.services_involved),
    ]
    return ". ".join(p for p in parts if p and p.strip())


def _build_index(model: Any) -> tuple[str, list[dict], Any]:
    """Embed the whole corpus once. Vectors are L2-normalised so cosine is a dot."""
    global _INDEX
    if _INDEX is not None and _INDEX[0] == _MODEL_NAME:
        return _INDEX
    records = [r for r in load_corpus() if _document_text(r).strip()]
    vectors = model.encode(
        [_document_text(r) for r in records],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    _INDEX = (_MODEL_NAME, records, vectors)
    logger.info("incident_history: embedded %d incident(s) for semantic retrieval", len(records))
    return _INDEX


# ─── warm-up ────────────────────────────────────────────────────────────────
#
# Measured cold cost on this machine: ~18.5s for the first search (torch import,
# model construction, then embedding 27 documents). Warm searches are 16-31ms.
#
# The chain guards each provider with AIOPS_INCIDENT_HISTORY_TIMEOUT (3s) and a 3s
# total budget, so a cold search does not merely run slowly — it is cancelled,
# recorded as a failure, and opens the provider's 30s circuit breaker. Every
# process restart would therefore cost the semantic tier its first several
# correlations while silently answering from the mock instead.
#
# So the load never happens on the request path. `warm()` does it off-thread, and
# a search that arrives before the model is ready returns UNAVAILABLE immediately
# rather than blocking: the chain falls through to the mock for that one call, and
# the next call is warm. UNAVAILABLE is the honest status — the corpus genuinely
# was not searched, which is not the same as searching it and finding nothing.
_warm_lock = threading.Lock()
_warm_thread: threading.Thread | None = None


def _warm_now() -> None:
    try:
        model = _get_model()
        if model is not None:
            _build_index(model)
    except Exception as exc:  # pragma: no cover - warming must never raise
        logger.warning("incident_history: embedding warm-up failed: %s", exc)


def warm(block: bool = False, timeout: float | None = None) -> None:
    """Load the model and embed the corpus, off the request path.

    Call at process start (the demo UI does this in its lifespan handler) so the
    first correlation is already warm. Idempotent and safe to call concurrently;
    ``block=True`` is for tests and CLI use where waiting is acceptable.
    """
    global _warm_thread
    if is_ready():
        return
    with _warm_lock:
        if _warm_thread is None or not _warm_thread.is_alive():
            _warm_thread = threading.Thread(
                target=_warm_now, name="incident-history-embed-warm", daemon=True
            )
            _warm_thread.start()
        thread = _warm_thread
    if block:
        thread.join(timeout)


def get_shared_model() -> Any | None:
    """Public accessor for the same lazily-loaded sentence-transformers model
    this module uses for the truth-file corpus — so a second consumer
    (agents/rca_agent/incident_rag.py, embedding a *different* corpus of
    persisted RCA verdicts) does not load a second copy of the model into
    memory. Returns ``None`` with the same tri-state semantics as the
    private ``_get_model()`` this wraps — never raises."""
    return _get_model()


def is_ready() -> bool:
    """True when a search can be served from cache without loading anything."""
    return bool(_MODEL) and _INDEX is not None and _INDEX[0] == _MODEL_NAME


def reset_index_for_tests() -> None:
    """Test seam — module-level caches would otherwise leak across tests."""
    global _MODEL, _INDEX, _UNAVAILABLE_REASON, _warm_thread
    _MODEL = None
    _INDEX = None
    _UNAVAILABLE_REASON = ""
    _warm_thread = None


class EmbeddingIncidentHistoryProvider(IncidentHistoryProvider):
    """Cosine retrieval over an in-process embedding index of the truth-file corpus."""

    name = "embedding"

    def health(self) -> tuple[bool, str]:
        # Reports on the model only. Deliberately does not embed the corpus: a
        # health probe that spends seconds building an index is a health probe
        # nobody calls.
        try:
            if _get_model() is None:
                return False, f"embedding model unavailable ({_UNAVAILABLE_REASON})"
            return True, f"embedding model {_MODEL_NAME} loaded ({len(load_corpus())} incidents)"
        except Exception as exc:  # pragma: no cover - health must never raise
            return False, f"{exc.__class__.__name__}: {exc}"

    def search(self, query: RetrievalQuery) -> RetrievalResult:
        started = time.monotonic()

        def elapsed() -> float:
            return (time.monotonic() - started) * 1000.0

        try:
            # Cold path never loads inline — see the warm-up block above. Loading
            # here would exceed the chain's 3s guard, be cancelled as a failure,
            # and open a 30s breaker on a tier that was merely starting up.
            if not is_ready():
                if _MODEL is False:
                    # Already attempted and genuinely broken: report the reason
                    # rather than promising a warm-up that will never succeed.
                    return RetrievalResult(
                        provider=self.name,
                        status=RetrievalStatus.UNAVAILABLE,
                        note=(
                            f"embedding model unavailable ({_UNAVAILABLE_REASON}); "
                            "install with `uv sync --extra embeddings`"
                        ),
                        latency_ms=elapsed(),
                    )
                warm()
                # Never EMPTY: the corpus was not searched at all, and reporting
                # "found nothing" would be a claim we have not earned.
                return RetrievalResult(
                    provider=self.name,
                    status=RetrievalStatus.UNAVAILABLE,
                    note=(
                        "embedding index still warming (first load takes ~20s); "
                        "retry shortly for semantic retrieval"
                    ),
                    latency_ms=elapsed(),
                )

            model = _get_model()
            _, records, vectors = _build_index(model)
            corpus_size = len(records)
            if corpus_size == 0:
                return RetrievalResult(
                    provider=self.name,
                    status=RetrievalStatus.EMPTY,
                    note="incident corpus is empty; no truth files were loaded",
                    corpus_size=0,
                    latency_ms=elapsed(),
                )

            text = _query_text(query)
            if not text.strip():
                return RetrievalResult(
                    provider=self.name,
                    status=RetrievalStatus.EMPTY,
                    note="query carried no service, signatures or services to embed",
                    corpus_size=corpus_size,
                    latency_ms=elapsed(),
                )

            qvec = model.encode([text], normalize_embeddings=True, show_progress_bar=False)[0]
            floor = max(query.min_similarity, _SEMANTIC_FLOOR)

            scored: list[tuple[float, dict]] = []
            for record, vec in zip(records, vectors, strict=True):
                # Both sides are L2-normalised, so the dot product IS cosine.
                score = float(sum(a * b for a, b in zip(qvec, vec, strict=True)))
                # IncidentMatch bounds similarity_score to [0, 1]; cosine is
                # [-1, 1] and an opposed pair is not "negatively similar", it is
                # simply not similar.
                score = max(0.0, min(1.0, score))
                if score >= floor:
                    scored.append((score, record))

            # Sort by score, then incident_id so equal scores are deterministic
            # rather than dependent on corpus file order.
            scored.sort(key=lambda t: (-t[0], t[1]["incident_id"]))
            top = scored[: max(1, query.limit)]

            if not top:
                return RetrievalResult(
                    provider=self.name,
                    status=RetrievalStatus.EMPTY,
                    note=(
                        f"no incident in a corpus of {corpus_size} reached the semantic "
                        f"floor of {floor:.2f}"
                    ),
                    corpus_size=corpus_size,
                    latency_ms=elapsed(),
                )

            matches = [self._to_match(score, record, query) for score, record in top]
            return RetrievalResult(
                provider=self.name,
                status=RetrievalStatus.MATCHED,
                matches=matches,
                corpus_size=corpus_size,
                note=f"semantic match over {corpus_size} incident(s), floor {floor:.2f}",
                latency_ms=elapsed(),
            )
        except Exception as exc:
            # search() must not raise: the contract turns every failure into a
            # status so a retrieval fault cannot break the correlation.
            logger.warning("incident_history: embedding search failed: %s", exc)
            return RetrievalResult(
                provider=self.name,
                status=RetrievalStatus.FAILED,
                error=f"{exc.__class__.__name__}: {exc}",
                latency_ms=elapsed(),
            )

    def _to_match(self, score: float, record: dict, query: RetrievalQuery) -> IncidentMatch:
        # Lexical overlap is reported alongside the semantic score even though it
        # did not drive it. A cosine number alone is unauditable; showing that a
        # 0.71 match shares zero signatures is what lets a reader judge whether
        # the model found a real analogue or a stylistic one.
        sig_hits = overlap(query.signatures, record.get("signatures") or [])
        svc_hits = overlap(query.services_involved, record.get("services") or [])
        topo_hits = overlap(query.topology, record.get("topology") or [])

        resolution = None
        if record.get("recorded_cause") or record.get("resolution_summary"):
            resolution = ResolutionMetadata(
                resolved=bool(record.get("resolution_summary")),
                resolution_summary=record.get("resolution_summary"),
                recorded_cause=record.get("recorded_cause"),
            )

        shared = (
            f"{len(sig_hits)} shared signature(s)"
            if sig_hits
            else "no shared signatures — matched on meaning alone"
        )
        return IncidentMatch(
            incident_id=str(record["incident_id"]),
            similarity_score=score,
            title=record.get("title"),
            # occurred_at is left to pydantic's parsing; the ecommerce family
            # carries no date and passes None rather than a fabricated one.
            occurred_at=record.get("occurred_at"),
            matching_signatures=sig_hits,
            matching_services=svc_hits,
            matching_topology=topo_hits,
            resolution=resolution,
            provider=self.name,
            match_explanation=(
                f"cosine {score:.2f} against the recorded cause and signals "
                f"({record.get('source', 'unknown')} corpus); {shared}"
            ),
        )
