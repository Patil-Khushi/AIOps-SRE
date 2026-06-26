"""Notification Router agent (RA-005) — Reactive-Active phase.

**Individually sellable unit.** A customer can license and deploy RA-005 on its
own to route one notification per incident (page on-call / chat the team /
daytime channel / noise bucket) without the war-room half.

The implementation lives in :mod:`agents.notification_assembler` (the shared
module that also powers the integrated one-message flow). This package is a thin
wrapper that exposes RA-005's original standalone contract and delegates to it,
so there is a single source of truth for the routing logic.

Public surface::

    from agents.notification_router import RoutingDecision, decide, route, run
"""

from __future__ import annotations

from .agent import decide, route, run
from .models import RoutingDecision, RoutingOutcome

__all__ = ["RoutingDecision", "RoutingOutcome", "decide", "route", "run"]
