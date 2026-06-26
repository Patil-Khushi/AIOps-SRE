"""Models for the Notification Router (RA-005).

Re-exported from :mod:`agents.notification_assembler.models` — the merged
module owns the canonical shapes. Keeping this module lets RA-005 be imported
and deployed as a standalone unit (``from agents.notification_router.models
import RoutingDecision``) without reaching into the shared package.
"""

from __future__ import annotations

from agents.notification_assembler.models import RoutingDecision, RoutingOutcome

__all__ = ["RoutingDecision", "RoutingOutcome"]
