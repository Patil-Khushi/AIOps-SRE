"""War-Room Assembler agent (RA-006) — Reactive-Active phase.

**Individually sellable unit.** A customer can license and deploy RA-006 on its
own to stand up the incident war room on Sev-1/Sev-2 (channel + on-call SME +
context pack + seed timeline) without the notification-routing half.

The implementation lives in :mod:`agents.notification_assembler` (the shared
module that also powers the integrated one-message flow). This package is a thin
wrapper that exposes RA-006's original standalone contract and delegates to it.

Public surface::

    from agents.war_room_assembler import decide, assemble, run
    from agents.war_room_assembler import WarRoomAssembly, WarRoomOutcome
"""

from __future__ import annotations

from .agent import assemble, decide, run
from .models import (
    ContextPackItem,
    InvitedSME,
    TimelineEvent,
    WarRoomAssembly,
    WarRoomOutcome,
)

__all__ = [
    "ContextPackItem",
    "InvitedSME",
    "TimelineEvent",
    "WarRoomAssembly",
    "WarRoomOutcome",
    "assemble",
    "decide",
    "run",
]
