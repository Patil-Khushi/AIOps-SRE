"""Incident Commander agent (RA-008, SRE) — Reactive-Active phase.

The SRE incident-coordination agent. It chains the Reactive-Active flow
(RA-001 → RA-002 → RA-003 → RA-005, via the INFRA-2 orchestrator seam) and the
RCA Agent into one coordinated Sev-1/Sev-2 response: timeline scribing, comms +
human-IC handoff through the chatops seam, and a facts-only postmortem seed.

RA-008 takes no destructive action — RCA fix-step execution stays on the
separately HITL-gated path (CLAUDE.md #3).

Public surface::

    from agents.incident_commander import (
        IncidentCommandResult, PostmortemSeed, TimelineEntry,
        command, run, reset_state,
    )
"""

from agents.incident_commander.agent import command, reset_state, run
from agents.incident_commander.models import (
    ICAuditMetadata,
    IncidentCommandResult,
    PostmortemSeed,
    TimelineEntry,
)

__all__ = [
    "ICAuditMetadata",
    "IncidentCommandResult",
    "PostmortemSeed",
    "TimelineEntry",
    "command",
    "reset_state",
    "run",
]
