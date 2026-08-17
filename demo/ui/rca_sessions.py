"""In-memory RCA chat sessions — bounded (LRU + TTL), not a new DB table.

Precedent for in-memory over ``aiops.state``: ``_HITL_OUTCOMES``
(``demo/ui/server.py``), ``_PUBLISH_OUTCOMES`` (``demo/ui/knowledge_routes.py``),
and the chatops history ring (``demo/ui/chatops_ws.py``) are three existing
process-global stores in this exact codebase; a fourth is idiomatic, a new
SQLModel table is not (POC scope discipline — CLAUDE.md).

Transcripts are lost on server restart. Grounding is not: ``rca_endpoint``
(``demo/ui/server.py``) calls ``aiops.state.repository.save_rca_result()``
whenever a request carries an ``incident_id``, so a post-restart conversation
can rehydrate its verdict/investigation from there (via
``GET /api/rca/chat/by-incident/{incident_id}``) even though the prior
transcript is gone. A persisted-transcript table is a documented future step —
useful once the Knowledge Synthesizer wants chat transcripts for postmortems —
not built now.

Own module, imported by both ``server.py`` and ``rca_chat_routes.py``, so
neither has to import the other (the same rule ``knowledge_routes.py``
follows).
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agents.rca_agent.chat import ChatTurn, GroundingPack
from agents.rca_agent.investigation.models import Investigation

# Bounds memory for a demo left running for a long time. Eviction is LRU by
# last touch (put/get both count), which in practice also acts as a soft TTL.
DEFAULT_MAX_SESSIONS = 64
DEFAULT_TTL_SECONDS = 3600.0


@dataclass
class RcaSession:
    run_id: str
    created_at: datetime
    last_used_at: datetime
    incident_id: str | None
    affected_service: str
    triage_verdict: dict[str, Any]
    verdict: dict[str, Any]
    investigation: Investigation | None
    grounding_pack: GroundingPack
    turns: list[ChatTurn] = field(default_factory=list)


def _max_sessions() -> int:
    raw = os.environ.get("AIOPS_RCA_CHAT_MAX_SESSIONS", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_SESSIONS
    except ValueError:
        return DEFAULT_MAX_SESSIONS


def _ttl_seconds() -> float:
    raw = os.environ.get("AIOPS_RCA_CHAT_TTL", "").strip()
    try:
        return float(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        return DEFAULT_TTL_SECONDS


class SessionStore:
    def __init__(self) -> None:
        self._sessions: OrderedDict[str, RcaSession] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, session: RcaSession) -> None:
        with self._lock:
            self._sessions[session.run_id] = session
            self._sessions.move_to_end(session.run_id)
            self._evict_locked()

    def get(self, run_id: str) -> RcaSession | None:
        with self._lock:
            self._evict_locked()
            session = self._sessions.get(run_id)
            if session is not None:
                self._sessions.move_to_end(run_id)
            return session

    def by_incident(self, incident_id: str) -> RcaSession | None:
        with self._lock:
            self._evict_locked()
            for session in reversed(self._sessions.values()):
                if session.incident_id == incident_id:
                    return session
            return None

    def drop(self, run_id: str) -> None:
        with self._lock:
            self._sessions.pop(run_id, None)

    def _evict_locked(self) -> None:
        """Must be called with ``self._lock`` held."""
        ttl = _ttl_seconds()
        now = datetime.now(UTC)
        stale = [
            run_id
            for run_id, session in self._sessions.items()
            if (now - session.last_used_at).total_seconds() > ttl
        ]
        for run_id in stale:
            self._sessions.pop(run_id, None)
        max_sessions = _max_sessions()
        while len(self._sessions) > max_sessions:
            self._sessions.popitem(last=False)

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._sessions.clear()


_STORE = SessionStore()


def get_session_store() -> SessionStore:
    return _STORE
