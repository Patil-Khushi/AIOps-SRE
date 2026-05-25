"""Notification Router agent (RA-005) — Reactive-Active phase.

Step 10 of the canonical alert→incident pipeline. Consumes a
``TriageVerdict`` from RA-001 (Alert Triage) and routes a notification
through the chatops seam based on severity, time of day, and ownership.

Public surface::

    from agents.notification_router import RoutingDecision, decide, route
"""

from .agent import decide, route, run
from .models import RoutingDecision, RoutingOutcome

__all__ = ["RoutingDecision", "RoutingOutcome", "decide", "route", "run"]
