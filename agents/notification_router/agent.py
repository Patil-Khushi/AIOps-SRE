"""Notification Router agent (RA-005) — standalone wrapper.

RA-005's original public surface (``decide`` / ``route`` / ``run``), delegating
to the shared :mod:`agents.notification_assembler` implementation. Deployed
alone it does exactly one job: route a single notification per verdict and emit
it through the chatops seam — no war room. The integrated product flow uses
``notification_assembler.notify`` (one combined message) instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agents.alert_triage import TriageVerdict
from agents.notification_assembler.agent import decide_routing, route
from agents.notification_assembler.models import RoutingDecision

__all__ = ["RoutingDecision", "decide", "route", "run"]


def decide(verdict: TriageVerdict, *, now: datetime | None = None) -> RoutingDecision:
    """Pure routing decision — no side effects. Safe to call in tests/evals."""
    return decide_routing(verdict, now=now)


def run(input_payload: dict) -> dict[str, Any]:
    """Eval-harness entry point. ``{"verdict": {...}, "now": "ISO8601"}`` →
    the ``RoutingDecision`` as a JSON-friendly dict. Pure — no chatops emit."""
    verdict = TriageVerdict.model_validate(input_payload["verdict"])
    now_raw = input_payload.get("now")
    now: datetime | None = None
    if isinstance(now_raw, str):
        now = datetime.fromisoformat(now_raw.replace("Z", "+00:00"))
    return decide_routing(verdict, now=now).model_dump(mode="json")
