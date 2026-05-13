"""Auto-Ticketing agent (RA-003) — Reactive-Active phase.

Consumes a ``TriageVerdict`` from RA-001 and:

    1. Decides whether to file a ticket (Suppressed verdicts are skipped).
    2. Maps severity to ServiceNow urgency (1=High / 2=Med / 3=Low).
    3. Calls ``itsm.incident.create`` via the registry — mock by default,
       real ServiceNow when ``AIOPS_USE_MOCK_ITSM=false``.
    4. Maps severity to a chat-ops channel and dispatches via ``notify.send``.
    5. Returns a ``TicketRecord`` capturing the outcome + decision_trace.

HITL: the ``itsm.incident.create`` capability is OPTIONAL-level in
``DEFAULT_LEVELS``, so the gate inside ``ToolRegistry.call()`` allows it
unattended unless a tenant flips ``tenant_requires_hitl``. The agent never
does HITL bookkeeping itself (CLAUDE.md non-negotiable #3).

Public surface::

    from agents.auto_ticketing import run, reset_state, TicketRecord
"""

from agents.auto_ticketing.agent import reset_state, run, ticket
from agents.auto_ticketing.models import TicketRecord

__all__ = ["TicketRecord", "reset_state", "run", "ticket"]
