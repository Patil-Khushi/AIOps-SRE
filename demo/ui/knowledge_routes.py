"""HTTP surface for the Knowledge Synthesizer (PRS-007).

Mounted into the demo server via a one-line ``app.include_router`` so the
synthesizer's endpoints live in their own module — it imports only ``agents``
and ``aiops``, never ``demo.ui.server``. That keeps the synthesizer fully
decoupled (CLAUDE.md: if it crashes or is slow, the Triage→…→RCA pipeline is
unaffected) and avoids a circular import.

Endpoints:

- ``POST /api/synthesize``            — run synthesis on a resolved-incident
                                        bundle; returns the SynthesisResult.
- ``GET  /api/kb``                    — list KB articles (filter by status/service).
- ``GET  /api/kb/{id}``               — one KB article.
- ``POST /api/kb/{id}/publish``       — request HITL-gated publication; returns
                                        an approval id (poll the outcome).
- ``GET  /api/kb/publish/outcome/{approval_id}`` — poll the publication outcome.

Publication is REQUIRED-HITL: the POST returns immediately with a pending
approval id while the gate blocks a background worker until a human approves —
the same fire-and-poll shape as the RCA apply-fix demo, but with its own
executor and outcome store so this module stays independent of the server.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agents.knowledge_synthesizer.agent import run as synthesize_run
from aiops.state import repository as repo

# Side-effect import: registers the seam.knowledge.publish tool so the gate
# can route publication through it.
from aiops.tools.knowledge import request_publish

logger = logging.getLogger(__name__)

router = APIRouter()

# Own executor + outcome store — deliberately not shared with the server's
# pools, so this surface can't be starved by (or starve) the core pipeline.
_PUBLISH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="kb-publish")
_PUBLISH_OUTCOMES: dict[str, dict[str, Any]] = {}


# ─── ticket-closed guard ──────────────────────────────────────────────────────
# Synthesis is the LAST step of the incident lifecycle — it must only run once
# the incident's ServiceNow ticket has been CLOSED (the 2nd HITL "close ticket?"
# approval, raised by the Resolution Verifier). In the real pipeline that
# ordering is guaranteed by the caller (verifier close branch / SNOW watcher's
# state 6,7 query). The direct ``POST /api/synthesize`` route has no such
# guarantee, so it enforces the invariant here — otherwise a draft can be created
# for an OPEN incident, skipping the remediation + close-ticket approvals and
# leaving only the publish gate. Opt out with ``bypass_ticket_check=true`` for an
# offline demo with no live ServiceNow (loudly logged).


def _extract_incident_id(bundle: dict[str, Any]) -> str:
    """Best-effort incident id from a synthesis bundle (mirrors the agent's
    ``_resolve_incident_id`` for the fields ServiceNow can be queried by)."""
    if bundle.get("incident_id"):
        return str(bundle["incident_id"])
    ticket = bundle.get("ticket") if isinstance(bundle.get("ticket"), dict) else {}
    for key in ("external_id", "id", "number"):
        if ticket and ticket.get(key):
            return str(ticket[key])
    return ""


def _ticket_is_closed(incident_id: str) -> bool | None:
    """Tri-state closure check via the ITSM seam.

    ``True``  — ticket confirmed Resolved(6)/Closed(7).
    ``False`` — ticket confirmed still open.
    ``None``  — indeterminate (no id, not found, or ServiceNow unavailable).

    The route treats anything other than ``True`` as "not closed" (fail-closed),
    so an unreachable ServiceNow can't be used to slip an open ticket through.
    """
    if not incident_id:
        return None
    try:
        from aiops.tools import get_registry

        res = get_registry().call("itsm.incident.get", number=incident_id, fields="number,state")
        if not getattr(res, "ok", False):
            return None
        rec = (getattr(res, "data", None) or {}).get("incident") or {}
        state = str(rec.get("state") or "")
        if not state:
            return None
        return state in {"6", "7"}
    except Exception:
        logger.exception("ticket-closed check failed for incident %s", incident_id)
        return None


# ─── synthesize ──────────────────────────────────────────────────────────────


@router.post("/api/synthesize", response_model=None)
async def synthesize_endpoint(
    bundle: dict[str, Any], bypass_ticket_check: bool = False
) -> dict[str, Any]:
    """Synthesize knowledge from a resolved-incident bundle.

    Body: ``{"triage_verdict": {...}, "rca_verdict": {...}, ...}``. Returns the
    SynthesisResult (postmortem + runbook suggestion + KB article persisted as
    ``pending_review``). Synthesis is sync + blocking (LLM), so we run it on a
    thread to keep the event loop free; failures are isolated to this request.

    By default this refuses to draft unless the incident's ServiceNow ticket is
    Resolved/Closed (the 2nd HITL close gate must have run first). Pass
    ``?bypass_ticket_check=true`` for an offline demo with no live ServiceNow —
    that skips the remediation, close-ticket, and ticket-closed checks and leaves
    only the publish approval."""
    if bypass_ticket_check:
        logger.warning(
            "synthesize: ticket-closed check BYPASSED for incident=%r — offline/demo "
            "path, skipping the remediation + close-ticket approvals",
            _extract_incident_id(bundle),
        )
    else:
        incident_id = _extract_incident_id(bundle)
        if _ticket_is_closed(incident_id) is not True:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"refusing to synthesize: incident {incident_id or '(none)'} is not "
                    "Resolved/Closed in ServiceNow — the close-ticket approval (2nd HITL) "
                    "hasn't run. Drive the fix → verify → close flow first (apply-fix → "
                    "'close ticket?' card), or resubmit with bypass_ticket_check=true for "
                    "an offline demo (which skips the remediation, close-ticket, and "
                    "ticket-closed checks)."
                ),
            )
    try:
        return await asyncio.to_thread(synthesize_run, bundle)
    except Exception as exc:
        logger.exception("synthesis failed for bundle keys=%s", list(bundle.keys()))
        raise HTTPException(status_code=500, detail=f"synthesis failed: {exc}") from exc


# ─── read ────────────────────────────────────────────────────────────────────


@router.get("/api/kb")
def list_kb(
    limit: int = 50, status: str | None = None, service: str | None = None
) -> dict[str, Any]:
    """Newest-first KB articles, optionally filtered by status/service."""
    if limit < 1 or limit > 500:
        limit = 50
    articles = repo.list_kb_articles(limit=limit, status=status, service=service)
    return {"count": len(articles), "articles": articles}


@router.get("/api/kb/{article_id}")
def get_kb(article_id: int) -> dict[str, Any]:
    article = repo.get_kb_article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"KB article {article_id} not found")
    return article


@router.post("/api/kb/reset")
def reset_kb() -> dict[str, Any]:
    """Demo-only: wipe all KB articles so a presentation can start from a clean
    table. Does not touch runbooks, verdicts, or any other state."""
    deleted = repo.delete_all_kb_articles()
    return {"deleted": deleted}


# ─── publish (HITL-gated) ────────────────────────────────────────────────────


class PublishRequest(BaseModel):
    # Optional override; defaults to the suggestion stashed at synthesis time.
    runbook: dict[str, Any] | None = None
    reason: str = Field("Approve publication of the synthesized KB article.")
    timeout_seconds: int = Field(120, ge=5, le=900)


@router.post("/api/kb/{article_id}/publish")
def publish_kb(article_id: int, req: PublishRequest) -> dict[str, Any]:
    """Kick off HITL-gated publication; return the approval id immediately.

    The gate posts an approve/deny prompt and blocks a background worker until
    a human resolves it — poll ``/api/kb/publish/outcome/{approval_id}``."""
    article = repo.get_kb_article(article_id)
    if article is None:
        raise HTTPException(status_code=404, detail=f"KB article {article_id} not found")

    runbook = req.runbook or (article.get("audit_metadata") or {}).get("runbook_suggestion")
    approval_id = uuid.uuid4().hex
    ctx: dict[str, Any] = {
        "approval_id": approval_id,
        "approval_timeout_seconds": req.timeout_seconds,
        "reason": req.reason,
        "capability": "knowledge.publish",
        "article_id": article_id,
    }

    def _run() -> None:
        _PUBLISH_OUTCOMES[approval_id] = request_publish(
            article_id=article_id, runbook=runbook, hitl_context=ctx
        )

    _PUBLISH_POOL.submit(_run)
    return {
        "approval_id": approval_id,
        "article_id": article_id,
        "status": "pending",
        "timeout_seconds": req.timeout_seconds,
    }


@router.get("/api/kb/publish/outcome/{approval_id}")
def publish_outcome(approval_id: str) -> dict[str, Any]:
    """Return the publication outcome once resolved, else ``status: pending``."""
    outcome = _PUBLISH_OUTCOMES.get(approval_id)
    if outcome is None:
        return {"approval_id": approval_id, "status": "pending"}
    return outcome


@router.get("/api/synthesizer/watcher-status")
def watcher_status() -> dict[str, Any]:
    """SNOW resolved-ticket watcher status: checkpoint, tickets processed,
    errors, circuit-breaker state, last poll time."""
    from agents.knowledge_synthesizer.snow_watcher import get_watcher

    return get_watcher().status()


@router.get("/api/verifier/status")
def verifier_status() -> dict[str, Any]:
    """Resolution verifier status: counts (verified/passed/failed/errors),
    stabilization windows, and the most recent verifications."""
    from agents.resolution_verifier.verifier import get_verifier

    return get_verifier().status()
