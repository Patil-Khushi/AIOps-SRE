"""War-Room Assembler agent (RA-006).

On Sev-1/Sev-2, stands up the incident war room: a chatops channel, the
on-call SME, a live context pack, and a seed timeline for RCA.

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
