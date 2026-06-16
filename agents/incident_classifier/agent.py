"""Incident Classifier agent (RA-002) — classification flow.

Entry point: ``classify(payload: ClassificationInput) -> Classification``.

Pipeline:

    1. Seed-if-empty  (idempotent; bootstraps the similarity store)
    2. Embed          (sentence-transformers; same model as RA-001)
    3. Search         (brute-force cosine top-K in SQLite, via aiops.state)
    4. Decide tier:
        - Tier 1  (similarity wins, no LLM call)   high similarity + top-3 agree
        - Tier 2  (LLM with retrieved evidence)    some matches, but tier-1 conditions miss
        - Tier 3  (LLM cold, few-shot only)        no matches above threshold
        - Tier 4  (keyword rule)                   LLM unavailable / unparseable
    5. Re-query CMDB  (RA-002 does NOT trust upstream CMDB fields — see CLAUDE.md #2)
    6. Assemble       (Classification + AuditMetadata with full decision trace)
    7. Persist        (new classification embedded back into the store so
                       future similar incidents have better matches —
                       this is how the agent "learns over time" without
                       any retraining)

Vendor-neutrality: imports ``aiops.llm``, ``aiops.tools``, ``aiops.state``
only. ``sentence_transformers`` and ``numpy`` are optional embedding deps,
not LLM/ITSM SDKs — they live behind a lazy loader so the agent degrades
gracefully when unavailable.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

# Side-effect: register mock CMDB / on-call / dependencies capabilities.
import aiops.tools.mock_providers  # noqa: F401
from agents.incident_classifier._seed import ensure_seeded
from agents.incident_classifier.models import (
    AuditMetadata,
    Classification,
    ClassificationInput,
)
from agents.incident_classifier.prompts import (
    CLASSIFY_PROMPT_USER,
    FEW_SHOT_EXAMPLES,
    SYSTEM_PROMPT,
)
from aiops.llm import Message
from aiops.llm import complete as llm_complete
from aiops.state.repository import (
    nearest_historical_incidents,
    save_historical_incident,
)
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# ─── tunables ───────────────────────────────────────────────────────────────
_TOP_K = 5
_MIN_SIMILARITY = 0.60
_TIER1_TOP_SIM = 0.85
_TIER1_AGREE_DEPTH = 3  # top-N must all share the same incident_type

_VALID_TYPES: set[str] = {
    "infrastructure",
    "application",
    "network",
    "external_dependency",
    "change_related",
}

# Keyword fallback for Tier-4 (LLM unavailable or unparseable). Lowercase
# substring match against the assembled embedding text.
_FALLBACK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "infrastructure": (
        "oom",
        "memory",
        "cpu",
        "disk",
        "pod-restart",
        "saturation",
        "throttling",
        "kubelet",
        "evicted",
    ),
    "application": (
        "exception",
        "nullpointer",
        "null pointer",
        "stack trace",
        "heap",
        "leak",
        "slow query",
    ),
    "network": (
        "dns",
        "envoy",
        "upstream",
        "tls",
        "connect_error",
        "handshake",
        "load balancer",
        "mesh",
    ),
    "external_dependency": (
        "stripe",
        "sendgrid",
        "twilio",
        "third-party",
        "vendor",
        "rate limit",
        "429",
    ),
    "change_related": (
        "deploy",
        "rollback",
        "release",
        "feature flag",
        "schema migration",
        "config change",
    ),
}

# ─── embedding model (lazy, optional) ───────────────────────────────────────
_EMBED_MODEL: Any = None  # None=unloaded, False=unavailable, else model object
_SEEDED = False


def _get_embed_model() -> Any | None:
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer

            _EMBED_MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            logger.info("RA-002 loaded sentence-transformers embedding model")
        except ImportError:
            logger.info(
                "sentence-transformers not installed; RA-002 similarity disabled "
                "(install via 'uv sync --extra embeddings')"
            )
            _EMBED_MODEL = False
    return _EMBED_MODEL if _EMBED_MODEL else None


def _embed(text: str) -> list[float] | None:
    """L2-normalized embedding as a list[float], or None if unavailable."""
    model = _get_embed_model()
    if model is None or not text:
        return None
    try:
        import numpy as np

        vec = model.encode(text, convert_to_numpy=True)
        norm = float(np.linalg.norm(vec))
        if norm < 1e-9:
            return None
        return (vec / norm).tolist()
    except Exception as exc:
        logger.warning("RA-002 embedding failed: %s", exc)
        return None


# ─── helpers ────────────────────────────────────────────────────────────────


def _build_embed_text(payload: ClassificationInput) -> str:
    a = payload.alert
    v = payload.triage_verdict
    parts = [
        f"service: {a.service}",
        f"severity: {v.severity}",
        f"summary: {v.alert_summary}",
        f"metric: {a.metric}",
    ]
    description = a.annotations.get("description") or a.annotations.get("summary")
    if description:
        parts.append(f"annotations: {description}")
    if a.labels:
        kv = ", ".join(f"{k}={val}" for k, val in sorted(a.labels.items()))
        parts.append(f"labels: {kv}")
    return " | ".join(parts)


def _format_similar_block(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return (
            "No similar past incidents found in the database — classify from "
            "first principles using the worked examples above."
        )
    lines = ["Similar past incidents (top matches by cosine similarity):"]
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"  {i}. [sim={c['similarity']:.2f}] type={c['incident_type']} "
            f"— {c['summary']} (root: {c['probable_root_cause']}) [{c['incident_key']}]"
        )
    return "\n".join(lines)


def _parse_llm_classification(text: str) -> dict[str, Any] | None:
    """Parse the strict 5-field response. Returns None on any failure so the
    caller falls through to the keyword rule."""
    if not text:
        return None
    text = text.strip()
    if text.lower().startswith("[stub]"):
        return None

    fields: dict[str, str] = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key_norm = key.strip().lower().replace(" ", "_")
        fields[key_norm] = val.strip()

    itype = fields.get("incident_type", "").lower()
    if itype not in _VALID_TYPES:
        return None

    try:
        confidence = float(fields.get("confidence", "0.5"))
    except ValueError:
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    rationale = fields.get("rationale", "").strip()
    if not rationale:
        rationale = "classified by RA-002 LLM"

    return {
        "incident_type": itype,
        "confidence": confidence,
        "probable_root_cause": fields.get("probable_root_cause", "").strip() or "unknown",
        "rationale": rationale[:300],
        "tags": [t.strip() for t in fields.get("tags", "").split(",") if t.strip()][:8],
    }


def _llm_classify(
    payload: ClassificationInput,
    candidates: list[dict[str, Any]],
    trace: list[str],
) -> dict[str, Any] | None:
    similar_block = _format_similar_block(candidates)
    a = payload.alert
    v = payload.triage_verdict
    user_prompt = CLASSIFY_PROMPT_USER.format(
        similar_incidents_block=similar_block,
        service=a.service,
        severity=v.severity,
        alert_summary=v.alert_summary,
        metric=a.metric,
        value=a.value,
        threshold=a.threshold,
        annotations=a.annotations or {},
        labels=a.labels or {},
    )
    try:
        # See RA-001 prompt budgeting note: reasoning models burn tokens
        # before emitting text, so we give a generous floor.
        resp = llm_complete(
            messages=[
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=FEW_SHOT_EXAMPLES + "\n\n" + user_prompt),
            ],
            temperature=0.2,
            max_tokens=1200,
        )
    except Exception as exc:
        trace.append(f"LLM error: {type(exc).__name__}")
        return None

    parsed = _parse_llm_classification(resp.text or "")
    if parsed is None:
        trace.append("LLM response unparseable (stub provider or malformed output)")
    return parsed


def _rule_based_fallback(text: str, trace: list[str]) -> dict[str, Any]:
    lowered = text.lower()
    scores: dict[str, int] = {}
    for itype, kws in _FALLBACK_KEYWORDS.items():
        scores[itype] = sum(1 for kw in kws if kw in lowered)
    best = max(scores, key=lambda k: scores[k])
    matched = scores[best]
    if matched == 0:
        best = "application"  # broadest default
    trace.append(f"Tier-4 keyword fallback chose {best} (keyword matches: {matched})")
    return {
        "incident_type": best,
        "confidence": 0.35 if matched > 0 else 0.25,
        "probable_root_cause": "unable to determine from available signals",
        "rationale": f"keyword fallback after LLM unavailable; matched {matched} keyword(s) for {best}",
        "tags": [],
    }


def _decide(
    payload: ClassificationInput,
    candidates: list[dict[str, Any]],
    embed_text: str,
    trace: list[str],
) -> dict[str, Any]:
    """Returns {incident_type, confidence, probable_root_cause, rationale, tags}.

    See module docstring for the 4-tier policy."""
    # Tier 1 — similarity wins
    if candidates and candidates[0]["similarity"] >= _TIER1_TOP_SIM:
        top_n = candidates[:_TIER1_AGREE_DEPTH]
        types = {c["incident_type"] for c in top_n}
        if len(top_n) >= _TIER1_AGREE_DEPTH and len(types) == 1:
            top_type = next(iter(types))
            top_sim = candidates[0]["similarity"]
            confidence = round(min(0.95, 0.70 + 0.25 * top_sim), 3)
            trace.append(
                f"Tier-1 (similarity wins): top-{_TIER1_AGREE_DEPTH} all type={top_type}, "
                f"top sim {top_sim:.2f}, confidence {confidence:.2f}"
            )
            return {
                "incident_type": top_type,
                "confidence": confidence,
                "probable_root_cause": candidates[0]["probable_root_cause"],
                "rationale": (
                    f"matches {_TIER1_AGREE_DEPTH} past {top_type} incidents at high similarity "
                    f"(top {top_sim:.2f}); no LLM call needed"
                ),
                "tags": list(candidates[0].get("tags", []))[:8],
            }

    # Tier 2 / 3 — LLM
    if candidates:
        trace.append(
            f"Tier-2 (LLM with evidence): {len(candidates)} candidates above "
            f"sim>={_MIN_SIMILARITY:.2f}, top sim {candidates[0]['similarity']:.2f}"
        )
        tier = 2
    else:
        trace.append("Tier-3 (LLM cold): no historical candidates above threshold")
        tier = 3

    llm = _llm_classify(payload, candidates, trace)
    if llm is not None:
        if tier == 2:
            llm["confidence"] = max(0.55, min(0.85, llm["confidence"]))
        else:
            llm["confidence"] = max(0.40, min(0.65, llm["confidence"]))
        trace.append(
            f"LLM classified as {llm['incident_type']}, confidence clamped to {llm['confidence']:.2f}"
        )
        return llm

    # Tier 4 — keyword fallback
    return _rule_based_fallback(embed_text, trace)


def _cmdb_lookup(service: str, trace: list[str]) -> tuple[str, str | None]:
    registry = get_registry()
    try:
        res = registry.call("itsm.cmdb.lookup", service=service)
    except KeyError:
        trace.append("itsm.cmdb.lookup capability not registered; defaulted to Platform On-Call")
        return "Platform On-Call", None
    except Exception as exc:
        trace.append(f"CMDB lookup error: {type(exc).__name__}")
        return "Platform On-Call", None
    if res.ok and res.data:
        team = res.data.get("team") or "Platform On-Call"
        runbook = res.data.get("runbook")
        trace.append(f"CMDB lookup: team={team}, runbook={'set' if runbook else 'none'}")
        return team, runbook
    trace.append("CMDB lookup returned empty; defaulted to Platform On-Call")
    return "Platform On-Call", None


def _oncall_lookup(team: str, trace: list[str], *, service: str | None = None) -> str | None:
    try:
        # ``service`` lets the DB provider apply sticky assignment, keeping
        # RA-002's on_call_engineer consistent with the engineer RA-001
        # already named on the verdict. The mock provider ignores it.
        res = get_registry().call("oncall.schedule.lookup", team=team, service=service)
    except KeyError:
        trace.append("oncall.schedule.lookup capability not registered; no engineer assigned")
        return None
    except Exception as exc:
        trace.append(f"on-call lookup error: {type(exc).__name__}")
        return None
    if res.ok and res.data:
        engineer = res.data.get("engineer_email")
        if engineer:
            trace.append(f"on-call lookup: engineer={engineer}")
            return engineer
    return None


def _dependencies_lookup(service: str, trace: list[str]) -> list[str]:
    try:
        res = get_registry().call("itsm.cmdb.dependencies", service=service)
    except KeyError:
        trace.append("itsm.cmdb.dependencies capability not registered; dependencies empty")
        return []
    except Exception as exc:
        trace.append(f"dependencies lookup error: {type(exc).__name__}")
        return []
    if res.ok and res.data:
        deps = list(res.data.get("dependencies", []) or [])
        trace.append(f"dependencies lookup: {len(deps)} downstream service(s)")
        return deps
    return []


def _persist_classification(
    payload: ClassificationInput,
    classification: Classification,
    embed_text: str,
    embedding: list[float] | None,
) -> None:
    """Write this classification back to the historical store so future
    similar incidents have more (and more recent) evidence to match against.
    Silently no-ops if embedding is unavailable."""
    if not embedding:
        return
    try:
        save_historical_incident(
            incident_key=f"LIVE-{payload.alert.alert_id}",
            incident_type=classification.incident_type,
            affected_service=payload.alert.service,
            severity=payload.triage_verdict.severity,
            summary=payload.triage_verdict.alert_summary,
            probable_root_cause=classification.probable_root_cause,
            recommended_runbook=classification.recommended_runbook,
            tags=list(classification.tags),
            embedding=embedding,
            embedding_text=embed_text,
            source="live",
            created_at=classification.audit_metadata.created_at,
        )
    except Exception as exc:
        logger.warning("RA-002 failed to persist classification: %s", exc)


def reset_for_tests() -> None:
    """Wipe the embedding-model cache + seed flag. For evals/tests that need
    a clean slate."""
    global _EMBED_MODEL, _SEEDED
    _EMBED_MODEL = None
    _SEEDED = False


def reset_state() -> None:
    """Eval-harness hook. Wipes live historical-incident rows from prior cases
    so each case starts from the seeded baseline, and resets the in-memory
    seed flag so ``ensure_seeded`` re-runs (idempotent — it sees the existing
    seed rows and no-ops)."""
    from aiops.state.repository import delete_live_historical_incidents

    delete_live_historical_incidents()
    reset_for_tests()


def _synthesize_verdict(alert: Any) -> Any:
    """Build a minimal deterministic ``TriageVerdict`` from an alert. Used by
    the eval ``run()`` to feed RA-002 without depending on RA-001's LLM-driven
    severity classification or summary generation — keeps the classifier eval
    isolated and fast."""
    from datetime import datetime

    from agents.alert_triage.models import AuditMetadata as TriageAudit
    from agents.alert_triage.models import TriageVerdict

    sev = "Sev-2"
    hint = (alert.severity_hint or "").lower()
    if any(t in hint for t in ("critical", "p1", "sev-1")):
        sev = "Sev-1"
    elif any(t in hint for t in ("warning", "p3", "sev-3")):
        sev = "Sev-3"
    elif any(t in hint for t in ("info", "low", "p4", "sev-4")):
        sev = "Sev-4"
    elif any(t in hint for t in ("high", "p2", "sev-2")):
        sev = "Sev-2"

    summary = (
        alert.annotations.get("description")
        or alert.annotations.get("summary")
        or f"{alert.service} {alert.metric} alert"
    )
    return TriageVerdict(
        affected_service=alert.service,
        severity=sev,  # type: ignore[arg-type]
        confidence_score=0.8,
        alert_summary=summary,
        assigned_team="Platform On-Call",
        duplicate_alert_count=1,
        status="Active",
        audit_metadata=TriageAudit(
            created_at=datetime.now(UTC),
            source_alerts=[alert.alert_id],
        ),
    )


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out. Takes an alert payload,
    synthesizes a minimal verdict, runs RA-002 ``classify``, returns the
    classification as a JSON-serializable dict."""
    from agents.alert_triage.models import Alert

    alert = Alert(**input)
    verdict = _synthesize_verdict(alert)
    result = classify(ClassificationInput(alert=alert, triage_verdict=verdict))
    return result.model_dump(mode="json")


# ─── entry point ────────────────────────────────────────────────────────────


def classify(payload: ClassificationInput) -> Classification:
    """Classify a triaged incident. Returns a structured ``Classification``.

    Read-only with respect to external systems beyond the CMDB / on-call
    lookups it owns. Does not open tickets, page anyone, or apply HITL gates —
    those are platform / downstream concerns.
    """
    global _SEEDED
    trace: list[str] = []
    now = datetime.now(UTC)

    # Stage 1 — seed if empty (idempotent; runs once per process)
    if not _SEEDED:
        inserted = ensure_seeded(_embed)
        _SEEDED = True
        if inserted > 0:
            trace.append(f"seeded historical store with {inserted} incident(s)")

    # Stage 2 — embed
    embed_text = _build_embed_text(payload)
    trace.append(f"built embedding text ({len(embed_text)} chars)")
    embedding = _embed(embed_text)
    if embedding is None:
        trace.append("embedding unavailable; similarity search will be skipped")

    # Stage 3 — nearest-K search
    candidates: list[dict[str, Any]] = []
    if embedding is not None:
        candidates = nearest_historical_incidents(
            embedding=embedding, k=_TOP_K, min_similarity=_MIN_SIMILARITY
        )
        trace.append(f"retrieved {len(candidates)} candidate(s) above sim>={_MIN_SIMILARITY:.2f}")

    # Stage 4 — decide
    decision = _decide(payload, candidates, embed_text, trace)

    # Stage 5 — CMDB re-query (independent — choice D)
    team, cmdb_runbook = _cmdb_lookup(payload.alert.service, trace)
    engineer = _oncall_lookup(team, trace, service=payload.alert.service)
    dependencies = _dependencies_lookup(payload.alert.service, trace)

    # Runbook preference: similar-incident match wins over CMDB default.
    similar_runbook = next(
        (
            c["recommended_runbook"]
            for c in candidates
            if c.get("recommended_runbook") and c["incident_type"] == decision["incident_type"]
        ),
        None,
    )
    final_runbook = similar_runbook or cmdb_runbook
    if similar_runbook and similar_runbook != cmdb_runbook:
        trace.append(f"runbook overridden from same-type similar incident: {similar_runbook}")

    # Stage 6 — assemble
    audit = AuditMetadata(
        created_at=now,
        created_by="RA-002",
        decision_trace=trace,
        similar_incidents=[
            {
                "incident_key": c["incident_key"],
                "incident_type": c["incident_type"],
                "similarity": round(float(c["similarity"]), 3),
                "summary": c.get("summary", ""),
            }
            for c in candidates
        ],
    )

    classification = Classification(
        incident_type=decision["incident_type"],  # type: ignore[arg-type]
        confidence=round(float(decision["confidence"]), 3),
        rationale=decision["rationale"],
        tags=list(decision["tags"]),
        probable_root_cause=decision["probable_root_cause"],
        routing_team=team,
        on_call_engineer=engineer,
        recommended_runbook=final_runbook,
        dependencies=dependencies,
        similar_incident_ids=[c["incident_key"] for c in candidates],
        audit_metadata=audit,
    )

    # Stage 7 — persist back (the "learns over time" mechanism)
    _persist_classification(payload, classification, embed_text, embedding)

    return classification
