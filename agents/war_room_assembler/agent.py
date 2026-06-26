"""War-Room Assembler agent (RA-006) — standalone wrapper.

RA-006's original public surface (``decide`` / ``assemble`` / ``run``),
delegating to the shared :mod:`agents.notification_assembler` implementation.
Deployed alone it does exactly one job: on Sev-1/Sev-2, stand up the war room
(channel + on-call SME + context pack + seed timeline) and post the opening to
its channel. The integrated product flow uses ``notification_assembler.notify``
(one combined message) instead.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agents.alert_triage import TriageVerdict
from agents.notification_assembler.agent import assemble, decide_war_room
from agents.notification_assembler.models import WarRoomAssembly

__all__ = ["WarRoomAssembly", "assemble", "decide", "run"]


def decide(verdict: TriageVerdict, *, now: datetime | None = None) -> WarRoomAssembly:
    """Pure assembly decision — no side effects. ``assembled=False`` for
    Sev-3/Sev-4 or Suppressed verdicts."""
    return decide_war_room(verdict, now=now)


def run(input_payload: dict) -> dict[str, Any]:
    """Eval-harness entry point. ``{"verdict": {...}, "now": "ISO8601"}`` →
    the ``WarRoomAssembly`` as a JSON-friendly dict. Pure — no chatops emit."""
    verdict = TriageVerdict.model_validate(input_payload["verdict"])
    now_raw = input_payload.get("now")
    now: datetime | None = None
    if isinstance(now_raw, str):
        now = datetime.fromisoformat(now_raw.replace("Z", "+00:00"))
    return decide_war_room(verdict, now=now).model_dump(mode="json")
