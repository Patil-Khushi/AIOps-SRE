"""Notification Assembler agent (RA-005+006) — Reactive-Active phase.

Merges the former Notification Router (RA-005) and War-Room Assembler (RA-006):
routes one notification per incident and, on Sev-1/Sev-2, stands up the war
room and folds its join link into that same message.

Public surface::

    from agents.notification_assembler import decide, notify, run
    from agents.notification_assembler import NotificationAssembly, NotificationOutcome
    from agents.notification_assembler import RoutingDecision, WarRoomAssembly
"""

from __future__ import annotations

from .agent import (
    assemble,
    assemble_war_room,
    decide,
    decide_routing,
    decide_war_room,
    notify,
    route,
    run,
)
from .models import (
    ContextPackItem,
    InvitedSME,
    NotificationAssembly,
    NotificationOutcome,
    RoutingDecision,
    RoutingOutcome,
    TimelineEvent,
    WarRoomAssembly,
    WarRoomOutcome,
)

__all__ = [
    "ContextPackItem",
    "InvitedSME",
    "NotificationAssembly",
    "NotificationOutcome",
    "RoutingDecision",
    "RoutingOutcome",
    "TimelineEvent",
    "WarRoomAssembly",
    "WarRoomOutcome",
    "assemble",
    "assemble_war_room",
    "decide",
    "decide_routing",
    "decide_war_room",
    "notify",
    "route",
    "run",
]
