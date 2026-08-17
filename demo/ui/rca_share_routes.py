"""Share a chat answer / postmortem draft to Microsoft Teams.

Deliberately its OWN module, not part of ``rca_chat_routes.py``: that file is
scanned by ``tests/test_rca_chat_boundary.py``'s AST check, which restricts
the chat surface to no ``aiops.tools.*`` import beyond one allowed read-only
RAG accessor — a boundary that exists to keep the chat Q&A path provably
incapable of executing anything or bypassing HITL. Sharing a message to Teams
is a plain, one-way notification (not remediation, not a tool the model can
invoke), but it still needs the chatops seam, so it lives here instead of
inside that restricted boundary.

Access control (explicitly, not implicitly)
--------------------------------------------
This endpoint has no auth check of its own — same POC posture as every other
``demo/ui/`` route (see ``server.py::_warn_if_approval_token_unset``, HITL-2
/ #102: "HITL web endpoints are unauthenticated" when no approval token is
configured). Concretely, anyone who can reach this FastAPI process can
post arbitrary ``title``/``body`` text into whatever channel
``AIOPS_TEAMS_WEBHOOK_URL`` targets. That is an acceptable blast radius for a
same-origin demo console — it can only ever post a message, never execute a
fix or touch HITL — but a hardened deployment must put real auth in front of
this route (or the whole ``demo/ui/`` app) before exposing it beyond
localhost/same-origin, exactly as the HITL approve/deny routes already note.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class ShareTeamsRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=4000)
    incident_id: str | None = None
    service: str | None = None


@router.post("/api/rca/chat/share-teams")
async def rca_chat_share_teams(req: ShareTeamsRequest) -> dict[str, Any]:
    """Post one chat answer/postmortem to the configured Teams webhook.

    Reuses the same ``TeamsWebhookAdapter`` the Notification Router (RA-005+006)
    posts through — no second Teams client, no direct ``httpx.post``. Degrades to
    an honest "not configured" response rather than a 500 when
    ``AIOPS_TEAMS_WEBHOOK_URL`` is unset, matching this platform's existing
    non-fatal-notification posture (``run_reactive_flow``'s routing failure is
    caught and non-fatal for the same reason).
    """
    from aiops.tools.chatops.adapters.teams import TeamsWebhookAdapter, is_teams_webhook_url
    from aiops.tools.chatops.models import ChatMessage, Severity

    webhook_url = os.environ.get("AIOPS_TEAMS_WEBHOOK_URL", "").strip()
    if not webhook_url or not is_teams_webhook_url(webhook_url):
        return {"sent": False, "reason": "AIOPS_TEAMS_WEBHOOK_URL is not configured"}

    msg = ChatMessage(
        channel="rca-chat",
        severity=Severity.INFO,
        title=req.title,
        body=req.body,
        incident_id=req.incident_id,
        service=req.service,
    )
    try:
        await asyncio.to_thread(TeamsWebhookAdapter(webhook_url).send, msg)
    except Exception as exc:
        logger.warning("rca chat: teams share failed: %s", exc)
        return {"sent": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"sent": True}
