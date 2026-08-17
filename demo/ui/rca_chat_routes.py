"""HTTP surface for the RCA chat — read-only Q&A over a frozen Investigation.

Mounted the same way ``demo/ui/knowledge_routes.py`` is: a one-line
``app.include_router`` in ``server.py``. Imports only ``agents`` and
``aiops`` (plus the sibling ``demo.ui.rca_sessions`` / ``demo.ui.rca_progress``
modules), never ``demo.ui.server`` — same layering rule, same reason (a
circular import, and keeping this surface's failure mode independent of the
core pipeline).

Endpoints:

- ``POST   /api/rca/chat``                    — one turn.
- ``GET    /api/rca/chat/{run_id}``            — full transcript + verdict
                                                 snapshot (page reload).
- ``GET    /api/rca/chat/by-incident/{id}``    — resolve the latest run_id for
                                                 an incident; also doubles as
                                                 the incident list's "is there
                                                 already an RCA for this?"
                                                 lookup via the stored verdict.
- ``DELETE /api/rca/chat/{run_id}``            — drop a conversation.

No new auth (consistent with the rest of this POC's posture — see
``demo/ui/server.py``'s HITL-2 note). Because this is a new *unauthenticated
LLM-cost* surface unlike most of the read-only API, three cheap caps apply
regardless: a per-session turn cap, a message-length cap, and one in-flight
turn per run_id (the last enforced by ``RcaSession`` being mutated under the
session store's own lock via get/put, not by this module).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agents.rca_agent import chat as rca_chat
from agents.rca_agent.models import RCAVerdict
from aiops.state import repository as state_repo
from demo.ui.rca_sessions import RcaSession, get_session_store

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_MESSAGE_LEN = 2000

# POST /api/rca's response (server.py) bolts these onto the RCAVerdict
# payload after remediation composition; RCAVerdict.model_config has
# extra="forbid", so a verdict round-tripped through this module (from the
# session store, or posted back by a client that got it from /api/rca) must
# have them stripped before re-validating — otherwise every rehydration and
# every history read fails.
_BOLT_ON_KEYS = ("remediation_options", "recommended_option_id")


def _verdict_from_dict(raw: dict[str, Any]) -> RCAVerdict:
    return RCAVerdict.model_validate({k: v for k, v in raw.items() if k not in _BOLT_ON_KEYS})


def _max_turns() -> int:
    """Read per call, not at import — same fix as ``agent._rca_provider()``
    (``AIOPS_CONTEXT_LAYER`` was the original bug this class of constant
    caused: a value baked in at import cannot be moved by ``monkeypatch``)."""
    raw = os.environ.get("AIOPS_RCA_CHAT_MAX_TURNS", "").strip()
    try:
        return int(raw) if raw else 50
    except ValueError:
        return 50


class RcaChatRequest(BaseModel):
    run_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)
    rca_verdict: dict[str, Any] | None = Field(
        None,
        description=(
            "Rehydrates a session that doesn't exist yet (server restart, or a verdict "
            "loaded from GET /api/verdicts rather than a live /api/rca run). Idempotent — "
            "ignored if a session for run_id already exists."
        ),
    )
    triage_verdict: dict[str, Any] | None = None
    incident_id: str | None = None


class RcaChatMessageOut(BaseModel):
    role: str
    text: str
    answer: dict[str, Any]


class RcaChatResponse(BaseModel):
    run_id: str
    message: RcaChatMessageOut
    verdict_snapshot: dict[str, Any]


def _snapshot(session: RcaSession, verdict: RCAVerdict) -> dict[str, Any]:
    """The server-read numbers the UI renders beside every answer — sourced
    from the stored session, never from the model's prose. The chat analogue
    of ``agent._authoritative_confidence``."""
    return {
        "affected_service": verdict.affected_service,
        "root_cause": verdict.root_cause,
        "root_cause_status": verdict.root_cause_status.value,
        "confidence_score": verdict.confidence_score,
        "selected_hypothesis_id": (
            session.investigation.selected_hypothesis_id if session.investigation else None
        ),
    }


def _hydrate_or_get(req: RcaChatRequest) -> RcaSession:
    store = get_session_store()
    session = store.get(req.run_id)
    if session is not None:
        return session
    if req.rca_verdict is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "no chat session for this run_id; POST /api/rca with this run_id first, "
                "or include rca_verdict on this request to rehydrate one"
            ),
        )
    verdict = _verdict_from_dict(req.rca_verdict)
    pack = rca_chat.build_grounding_pack(verdict, verdict.investigation, verdict.affected_service)
    now = datetime.now(UTC)
    session = RcaSession(
        run_id=req.run_id,
        created_at=now,
        last_used_at=now,
        incident_id=req.incident_id,
        affected_service=verdict.affected_service,
        triage_verdict=req.triage_verdict or {},
        verdict=req.rca_verdict,
        investigation=verdict.investigation,
        grounding_pack=pack,
    )
    store.put(session)
    return session


@router.post("/api/rca/chat")
async def rca_chat_send(req: RcaChatRequest) -> RcaChatResponse:
    session = _hydrate_or_get(req)
    if len(session.turns) >= _max_turns() * 2:
        raise HTTPException(status_code=429, detail=f"conversation exceeded {_max_turns()} turns")

    verdict = _verdict_from_dict(session.verdict)
    prior_turns = list(session.turns)
    session.turns.append(rca_chat.ChatTurn(role="user", text=req.message))

    result = await asyncio.to_thread(
        rca_chat.answer,
        session.grounding_pack,
        prior_turns,
        req.message,
        verdict,
        incident_id=session.incident_id,
    )

    session.turns.append(rca_chat.ChatTurn(role="assistant", text=result.answer))
    session.last_used_at = datetime.now(UTC)

    return RcaChatResponse(
        run_id=req.run_id,
        message=RcaChatMessageOut(
            role="agent", text=result.answer, answer=result.model_dump(mode="json")
        ),
        verdict_snapshot=_snapshot(session, verdict),
    )


@router.get("/api/rca/chat/{run_id}")
async def rca_chat_history(run_id: str) -> dict[str, Any]:
    session = get_session_store().get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no chat session for this run_id")
    verdict = _verdict_from_dict(session.verdict)
    return {
        "run_id": run_id,
        "messages": [t.model_dump(mode="json") for t in session.turns],
        "verdict_snapshot": _snapshot(session, verdict),
    }


@router.get("/api/rca/chat/by-incident/{incident_id}")
async def rca_chat_by_incident(incident_id: str) -> dict[str, Any]:
    """Resolves the latest live session for an incident, or falls back to the
    persisted verdict (``repository.save_rca_result``) — this is also what
    lets the incident list show "RCA ready" without re-running the agent."""
    session = get_session_store().by_incident(incident_id)
    if session is not None:
        return {"run_id": session.run_id, "has_session": True, "verdict": session.verdict}
    stored = state_repo.get_rca_result(incident_id)
    if stored is not None:
        return {"run_id": None, "has_session": False, "verdict": stored.get("verdict")}
    raise HTTPException(status_code=404, detail="no RCA verdict for this incident_id")


@router.delete("/api/rca/chat/{run_id}")
async def rca_chat_delete(run_id: str) -> dict[str, bool]:
    get_session_store().drop(run_id)
    return {"ok": True}
