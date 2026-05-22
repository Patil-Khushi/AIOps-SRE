"""Chatops seam — vendor-neutral notification routing.

Public API:

- ``ChatMessage`` / ``Severity`` — the canonical message ticket
- ``ChatOpsAdapter`` — protocol every sink implements
- ``ChatOpsClient`` — fans messages to all registered sinks
- ``get_client()`` — process-wide singleton accessor

D2 (WebSocket → React panel) and D3 (JSON audit log) plug their adapters
into ``get_client().register(...)`` at startup. Slack / Teams / PagerDuty
adapters land later without touching agent code.
"""

from __future__ import annotations

from .client import ChatOpsAdapter, ChatOpsClient, get_client
from .models import ChatMessage, InteractivePrompt, Severity, to_record

__all__ = [
    "ChatMessage",
    "ChatOpsAdapter",
    "ChatOpsClient",
    "InteractivePrompt",
    "Severity",
    "get_client",
    "to_record",
]
