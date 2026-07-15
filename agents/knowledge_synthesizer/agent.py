"""Knowledge Synthesizer Agent (PRS-007).

Entry point: ``synthesize(bundle, scenario_id=None) -> SynthesisResult``.

Pipeline (v0):

    1. Resolve the incident id + idempotency guard (already synthesized?).
    2. Reconstruct the timeline from cross-agent audit timestamps.
    3. Draft a postmortem â€” LLM pass with a deterministic fallback (CI / stub).
    4. Suggest a runbook (new or update) from the RCA fix steps.
    5. Build a KB article and REDACT PII/secrets before persisting.
    6. Quality-score + dedupe against existing articles (cosine when embeddings
       are available, signature overlap otherwise).
    7. Persist the article as ``pending_review``. Publication is platform-HITL-
       gated via the ``knowledge.publish`` (Required) capability and the
       ``seam.knowledge.publish`` tool (``aiops/tools/knowledge.py``), wired in
       this PR â€” synthesis only ever persists ``pending_review`` and physically
       cannot publish; a human approves before anything goes ``published``.

Vendor-neutrality: LLM via ``aiops.llm`` only; persistence via
``aiops.state.repository`` only; runbooks via the ``aiops.runbooks`` seam only.
No SDKs, no direct file or DB access.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agents.knowledge_synthesizer.models import (
    DedupDecision,
    KBArticle,
    Postmortem,
    ReviewStatus,
    RunbookSuggestion,
    SynthesisInput,
    SynthesisResult,
    TimelineEntry,
)
from agents.knowledge_synthesizer.prompts import POSTMORTEM_USER_V1, SYSTEM_PROMPT_V1
from agents.knowledge_synthesizer.redaction import RedactionReport, redact
from aiops import runbooks
from aiops.llm import Message
from aiops.llm import complete as llm_complete
from aiops.state import repository as repo

logger = logging.getLogger(__name__)

SEED_RUNBOOKS_DIR = Path(__file__).parent / "seed_runbooks"

# Cosine threshold above which two articles are "the same knowledge".
_DEDUP_COSINE = 0.9
# Jaccard threshold for the no-embeddings signature fallback.
_DEDUP_SIGNATURE = 0.6
# Candidate statuses dedup compares against (a rejected draft isn't knowledge).
_DEDUP_STATUSES = {"published", "pending_review"}


# â”€â”€â”€ embeddings (reused pattern from RA-002) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_EMBED_MODEL: Any = None  # None=unloaded, False=unavailable, else model object


def _get_embed_model() -> Any | None:
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer

            _EMBED_MODEL = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
            logger.info("PRS-007 loaded sentence-transformers embedding model")
        except Exception as exc:
            # Optional feature: any failure loading the embedding stack must fall
            # back to signature overlap, never break synthesis. Covers ImportError
            # (extra not installed) and OSError/others — e.g. a torch native DLL
            # that can't load because the MSVC++ runtime is missing on Windows
            # ([WinError 126] loading c10.dll).
            logger.info(
                "PRS-007 embedding model unavailable (%s: %s); dedup falls back to "
                "signature overlap (install via 'uv sync --extra embeddings')",
                exc.__class__.__name__,
                exc,
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
        logger.warning("PRS-007 embedding failed: %s", exc)
        return None


# â”€â”€â”€ small helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _normalize_service(service: str) -> str:
    s = (service or "").lower().strip()
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


def _significant_tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= 4}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _merge_redactions(*reports: RedactionReport) -> RedactionReport:
    """Sum findings across the per-field redaction reports (title/summary/body)
    so the audit record reflects everything scrubbed, not just one field."""
    merged: dict[str, int] = {}
    for rep in reports:
        for k, v in rep.findings.items():
            merged[k] = merged.get(k, 0) + v
    return RedactionReport(merged)


def _resolve_incident_id(parsed: SynthesisInput) -> str:
    """Best-effort stable id for the resolved incident â€” the idempotency key."""
    if parsed.incident_id:
        return parsed.incident_id
    ticket = parsed.ticket or {}
    for key in ("external_id", "id", "number"):
        if ticket.get(key):
            return str(ticket[key])
    tv = parsed.triage_verdict or {}
    if tv.get("incident_id"):
        return str(tv["incident_id"])
    # No real incident id anywhere. A service-only key would make two DIFFERENT
    # incidents on the same service collide (the 2nd would skip_idempotent and
    # never draft â€” silent data loss). Fold a stable per-incident discriminator
    # (alert summary + triage timestamp + resolved_at) so re-running the SAME
    # incident still matches, but different incidents on one service don't.
    # hashlib (not hash()) so the key is stable across process restarts.
    service = str(tv.get("affected_service") or "unknown")
    audit = tv.get("audit_metadata") if isinstance(tv.get("audit_metadata"), dict) else {}
    discriminator = "|".join(
        [
            str(tv.get("alert_summary") or ""),
            str((audit or {}).get("created_at") or ""),
            str(parsed.resolved_at or ""),
        ]
    )
    if not discriminator.strip("|"):
        # Truly id-less AND info-less input â€” don't fabricate a colliding key.
        raise ValueError(
            "cannot synthesize without an incident id or any discriminating "
            "context (incident_id / ticket id / alert_summary / timestamps)"
        )
    digest = hashlib.sha1(discriminator.encode("utf-8")).hexdigest()[:12]
    return f"incident:{service}:{digest}"


def _fix_steps(rca: dict[str, Any]) -> list[dict[str, Any]]:
    steps = rca.get("ranked_fix_steps") or []
    return [s for s in steps if isinstance(s, dict)]


def _fix_text(rca: dict[str, Any]) -> str:
    steps = _fix_steps(rca)
    if not steps:
        return "Manual investigation; no automated fix recorded."
    return "; ".join(str(s.get("description", "")).strip() for s in steps if s.get("description"))


def _ts_from_audit(obj: dict[str, Any] | None) -> str | None:
    if not isinstance(obj, dict):
        return None
    audit = obj.get("audit_metadata")
    if isinstance(audit, dict) and audit.get("created_at"):
        return str(audit["created_at"])
    if obj.get("created_at"):
        return str(obj["created_at"])
    return None


def _build_timeline(parsed: SynthesisInput) -> list[TimelineEntry]:
    """Reconstruct the timeline from the timestamps each upstream agent already
    stamped â€” RCA carries no timeline field, so we assemble it rather than
    change RCA's contract."""
    candidates = [
        (_ts_from_audit(parsed.triage_verdict), "Alert triaged", "RA-001"),
        (_ts_from_audit(parsed.classification), "Incident classified", "RA-002"),
        (_ts_from_audit(parsed.ticket), "Ticket created", "RA-003"),
        (_ts_from_audit(parsed.rca_verdict), "Root cause identified", "PRS-008"),
        (parsed.resolved_at, "Incident resolved", "PRS-007"),
    ]
    entries = [
        TimelineEntry(ts=ts, event=event, source_agent=src)
        for ts, event, src in candidates
        if ts is not None
    ]
    # ISO-8601 strings sort chronologically; entries without a ts are dropped.
    entries.sort(key=lambda e: e.ts or "")
    return entries


# â”€â”€â”€ postmortem drafting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(cleaned[start : i + 1])
                        return obj if isinstance(obj, dict) else None
                    except json.JSONDecodeError:
                        return None
    return None


def _impact_from_severity(severity: str) -> str:
    sev = (severity or "").lower()
    if "1" in sev or "crit" in sev:
        return "High â€” customer-facing functionality degraded or unavailable."
    if "2" in sev or "warn" in sev:
        return "Moderate â€” elevated latency/errors; partial user impact."
    return "Low â€” limited or no direct customer impact."


def _deterministic_postmortem(parsed: SynthesisInput, timeline: list[TimelineEntry]) -> Postmortem:
    tv = parsed.triage_verdict or {}
    rca = parsed.rca_verdict or {}
    service = str(rca.get("affected_service") or tv.get("affected_service") or "unknown")
    summary = str(tv.get("alert_summary") or f"{service} incident")
    return Postmortem(
        affected_service=service,
        what_broke=summary,
        root_cause=str(rca.get("root_cause") or "Root cause not recorded."),
        timeline=timeline,
        fix=_fix_text(rca),
        impact=_impact_from_severity(str(tv.get("severity") or "")),
        confidence_score=float(rca.get("confidence_score") or 0.0),
    )


def _llm_postmortem(
    parsed: SynthesisInput, timeline: list[TimelineEntry]
) -> tuple[Postmortem, list[str], str] | None:
    """Try to draft via the LLM. Returns (postmortem, tags, title) or None to
    signal the caller to use the deterministic fallback."""
    tv = parsed.triage_verdict or {}
    rca = parsed.rca_verdict or {}
    service = str(rca.get("affected_service") or tv.get("affected_service") or "unknown")
    fix_steps_rendered = (
        "\n".join(f"- {s.get('description', '')}" for s in _fix_steps(rca)) or "- (none recorded)"
    )
    timeline_rendered = "\n".join(f"- {e.ts or '?'}: {e.event}" for e in timeline) or "- (none)"
    user = POSTMORTEM_USER_V1.format(
        service=service,
        severity=str(tv.get("severity") or "unknown"),
        alert_summary=str(tv.get("alert_summary") or "(no summary)"),
        root_cause=str(rca.get("root_cause") or "(none)"),
        fix_steps=fix_steps_rendered,
        timeline=timeline_rendered,
    )
    try:
        resp = llm_complete(
            messages=[
                Message(role="system", content=SYSTEM_PROMPT_V1),
                Message(role="user", content=user),
            ],
            temperature=0.3,
            max_tokens=1200,
        )
    except Exception as exc:
        logger.warning("PRS-007 LLM call failed (%s); using deterministic fallback", exc)
        return None
    text = (resp.text or "").strip()
    if not text or text.startswith("[stub]"):
        return None
    raw = _extract_json_object(text)
    if raw is None:
        return None
    pm = Postmortem(
        affected_service=service,
        what_broke=str(raw.get("what_broke") or tv.get("alert_summary") or service),
        root_cause=str(raw.get("root_cause") or rca.get("root_cause") or ""),
        timeline=timeline,
        fix=str(raw.get("fix") or _fix_text(rca)),
        impact=str(raw.get("impact") or _impact_from_severity(str(tv.get("severity") or ""))),
        confidence_score=float(rca.get("confidence_score") or 0.0),
    )
    tags = [str(t).lower() for t in (raw.get("tags") or []) if str(t).strip()]
    title = str(raw.get("title") or f"Postmortem: {service}")
    return pm, tags, title


# â”€â”€â”€ runbook suggestion â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _runbook_body(pm: Postmortem, rca: dict[str, Any]) -> str:
    steps = _fix_steps(rca)
    resolution = (
        "\n".join(
            f"{i + 1}. [{s.get('action_type', 'manual')} Â· {s.get('blast_radius', 'medium')}] "
            f"{s.get('description', '')}"
            for i, s in enumerate(steps)
        )
        or "1. [manual] Investigate and remediate."
    )
    rollback = (
        "\n".join(f"{i + 1}. {s.get('rollback', 'N/A')}" for i, s in enumerate(steps)) or "1. N/A"
    )
    return (
        f"## Symptoms\n{pm.what_broke}\n\n"
        f"## Diagnosis\n{pm.root_cause}\n\n"
        f"## Resolution steps\n{resolution}\n\n"
        f"## Verification\nConfirm the affected metric returns to baseline and the "
        f"alert clears.\n\n"
        f"## Rollback\n{rollback}\n"
    )


def _runbook_suggestion(pm: Postmortem, rca: dict[str, Any]) -> RunbookSuggestion:
    """Suggest a new runbook, or an update to an existing one for the service.
    Reads the (seeded) library through the seam â€” does not write it."""
    existing = runbooks.search_runbooks(service=pm.affected_service)
    body = _runbook_body(pm, rca)
    title = f"{pm.affected_service} â€” {pm.what_broke[:60]}"
    if existing:
        target = existing[0]
        return RunbookSuggestion(
            mode="update", target_id=target.id, title=target.title, body_markdown=body
        )
    slug = "rb-" + re.sub(r"[^a-z0-9]+", "-", pm.affected_service.lower()).strip("-")
    return RunbookSuggestion(mode="new", target_id=slug, title=title, body_markdown=body)


# â”€â”€â”€ KB article + quality + dedup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _kb_body(pm: Postmortem) -> str:
    timeline = (
        "\n".join(f"- {e.ts or '?'}: {e.event}" for e in pm.timeline) or "- (timeline unavailable)"
    )
    return (
        f"## What broke\n{pm.what_broke}\n\n"
        f"## Root cause\n{pm.root_cause}\n\n"
        f"## Timeline\n{timeline}\n\n"
        f"## Fix\n{pm.fix}\n\n"
        f"## Impact\n{pm.impact}\n"
    )


def _quality_score(pm: Postmortem, tags: list[str]) -> float:
    score = 0.4
    if len(pm.root_cause) >= 40:
        score += 0.2
    if pm.fix and "no automated fix" not in pm.fix.lower():
        score += 0.2
    if len(pm.timeline) >= 2:
        score += 0.1
    if tags:
        score += 0.1
    return round(min(score, 1.0), 3)


def _dedup_lookup(
    *, service: str, dedup_text: str, embedding: list[float] | None
) -> tuple[int, float, str] | None:
    """Return (matched_id, similarity, method) for a near-duplicate, else None."""
    if embedding:
        hits = repo.nearest_kb_articles(
            embedding=embedding,
            k=1,
            min_similarity=_DEDUP_COSINE,
            statuses=_DEDUP_STATUSES,
        )
        if hits:
            return int(hits[0]["id"]), float(hits[0]["similarity"]), "embedding"
        return None
    # No embeddings: signature overlap within the same service.
    sig = _significant_tokens(dedup_text)
    best: tuple[int, float] | None = None
    for art in repo.list_kb_articles(limit=500):
        if art["status"] not in _DEDUP_STATUSES:
            continue
        if _normalize_service(art["service"]) != _normalize_service(service):
            continue
        other = _significant_tokens(art.get("embedding_text") or art.get("title") or "")
        j = _jaccard(sig, other)
        if j >= _DEDUP_SIGNATURE and (best is None or j > best[1]):
            best = (int(art["id"]), j)
    return (best[0], best[1], "signature") if best else None


# â”€â”€â”€ entry point â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def synthesize(bundle: dict[str, Any], *, scenario_id: str | None = None) -> SynthesisResult:
    """Synthesize knowledge from one resolved incident bundle."""
    parsed = SynthesisInput(**bundle)
    if scenario_id is None:
        scenario_id = parsed.scenario_id

    # Ensure the runbook library is populated so new-vs-update is meaningful.
    runbooks.ensure_seeded(SEED_RUNBOOKS_DIR)

    incident_id = _resolve_incident_id(parsed)
    rca = parsed.rca_verdict or {}
    timeline = _build_timeline(parsed)

    # Draft the postmortem (LLM, else deterministic).
    drafted = _llm_postmortem(parsed, timeline)
    if drafted is not None:
        postmortem, tags, title = drafted
    else:
        postmortem = _deterministic_postmortem(parsed, timeline)
        title = f"Postmortem: {postmortem.affected_service} â€” {postmortem.what_broke[:60]}"
        tags = _auto_tags(postmortem, parsed)

    quality = _quality_score(postmortem, tags)
    runbook = _runbook_suggestion(postmortem, rca)

    # REDACT every persisted field (title, summary, body) before anything is
    # stored, and MERGE the findings so the audit record covers all of them â€”
    # not just the body (review #4: a secret only in the title/summary was
    # scrubbed but left no trace in audit_metadata["redaction"]).
    red_title = redact(title)
    red_summary = redact(postmortem.what_broke)
    red_body = redact(_kb_body(postmortem))
    redaction = _merge_redactions(red_title.report, red_summary.report, red_body.report)

    article = KBArticle(
        incident_id=incident_id,
        title=red_title.text,
        summary=red_summary.text,
        body=red_body.text,
        service=postmortem.affected_service,
        tags=tags,
        quality_score=quality,
        status=ReviewStatus.PENDING_REVIEW,
        related_runbook_id=runbook.target_id,
    )

    dedup_text = f"{postmortem.affected_service} {postmortem.root_cause}"
    embedding = _embed(dedup_text)

    # Idempotency: same incident already synthesized â†’ don't create a second.
    existing = repo.find_kb_by_incident_id(incident_id)
    if existing is not None:
        dedup = DedupDecision(
            action="skip_idempotent",
            matched_article_id=int(existing["id"]),
            similarity=1.0,
            method="incident_id",
        )
        return _result(
            incident_id,
            postmortem,
            article,
            runbook,
            dedup,
            kb_article_id=int(existing["id"]),
            status=ReviewStatus(existing["status"]),
            quality=quality,
            redaction_summary=redaction.summary(),
        )

    # Dedup against existing knowledge (cosine, else signature).
    match = _dedup_lookup(
        service=postmortem.affected_service, dedup_text=dedup_text, embedding=embedding
    )
    if match is not None:
        matched_id, sim, method = match
        dedup = DedupDecision(
            action="duplicate",
            matched_article_id=matched_id,
            similarity=sim,
            method=method,  # type: ignore[arg-type]
        )
        return _result(
            incident_id,
            postmortem,
            article,
            runbook,
            dedup,
            kb_article_id=matched_id,
            status=ReviewStatus.PENDING_REVIEW,
            quality=quality,
            redaction_summary=redaction.summary(),
        )

    # No duplicate â†’ persist a new draft pending review.
    new_id = repo.save_kb_article(
        title=article.title,
        body=article.body,
        incident_id=incident_id,
        summary=article.summary,
        service=article.service,
        tags=article.tags,
        status=ReviewStatus.PENDING_REVIEW.value,
        quality_score=quality,
        related_runbook_id=runbook.target_id,
        embedding=embedding,
        embedding_text=dedup_text,
        audit_metadata={
            "created_by": "PRS-007",
            "scenario_id": scenario_id,
            "redaction": redaction.findings,
            "runbook_mode": runbook.mode,
            # Stash the suggestion so publication (a later, separate request)
            # can write the runbook without re-running synthesis.
            "runbook_suggestion": {
                "mode": runbook.mode,
                "target_id": runbook.target_id,
                "title": runbook.title,
                "body_markdown": runbook.body_markdown,
                "service": article.service,
            },
        },
    )
    dedup = DedupDecision(
        action="create",
        matched_article_id=None,
        similarity=0.0,
        method="embedding" if embedding else "signature",
    )
    return _result(
        incident_id,
        postmortem,
        article,
        runbook,
        dedup,
        kb_article_id=new_id,
        status=ReviewStatus.PENDING_REVIEW,
        quality=quality,
        redaction_summary=redaction.summary(),
    )


def _auto_tags(pm: Postmortem, parsed: SynthesisInput) -> list[str]:
    tags: list[str] = [_normalize_service(pm.affected_service)]
    cls = parsed.classification or {}
    tags.extend(str(t).lower() for t in (cls.get("tags") or []))
    # Surface a flagd flag mentioned in the root cause (e.g. productCatalogFailure).
    for m in re.findall(r"\b([a-z][A-Za-z]*Failure)\b", pm.root_cause):
        tags.append(m.lower())
    # Dedupe, keep order.
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _result(
    incident_id: str,
    postmortem: Postmortem,
    article: KBArticle,
    runbook: RunbookSuggestion,
    dedup: DedupDecision,
    *,
    kb_article_id: int | None,
    status: ReviewStatus,
    quality: float,
    redaction_summary: str,
) -> SynthesisResult:
    article = article.model_copy(update={"status": status})
    return SynthesisResult(
        incident_id=incident_id,
        affected_service=postmortem.affected_service,
        status=status,
        root_cause=postmortem.root_cause,
        dedup_action=dedup.action,
        runbook_mode=runbook.mode,
        related_runbook_id=runbook.target_id,
        kb_article_id=kb_article_id,
        quality_score=quality,
        redaction_summary=redaction_summary,
        created_at=datetime.now(UTC),
        postmortem=postmortem,
        kb_article=article,
        runbook_suggestion=runbook,
        dedup=dedup,
    )


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out shim around ``synthesize``."""
    parsed = SynthesisInput(**input)
    result = synthesize(input, scenario_id=parsed.scenario_id)
    return result.model_dump(mode="json")


def reset_state() -> None:
    """Eval-harness hook. The synthesizer's live state is the KB-article store;
    wipe it between golden cases so the idempotency/dedup guards start clean.
    Runbook seeds are left in place (ensure_seeded is idempotent)."""
    repo.delete_all_kb_articles()
