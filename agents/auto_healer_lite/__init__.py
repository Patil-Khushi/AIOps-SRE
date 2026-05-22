"""Auto-Healer-lite — the Phase-2 HITL demo agent (issue #77).

Not the real Auto-Healer from the catalog (PA-002).  Built specifically to
exercise the HITL approval flow end-to-end so the platform principle "HITL
is platform-enforced, not agent-enforced" (CLAUDE.md #3) becomes a runnable
demo rather than an architectural claim.

The agent recommends a deployment restart through the
``automation.runbook.execute`` capability, which is REQUIRED in the gate's
default level map.  The gate blocks the call, the approval registry posts
to chatops, a human approves via Slack or the dashboard, the action runs.

Public surface::

    from agents.auto_healer_lite import recommend_restart, RestartRecommendation
"""

from agents.auto_healer_lite.agent import recommend_restart
from agents.auto_healer_lite.models import RestartOutcome, RestartRecommendation

__all__ = ["RestartOutcome", "RestartRecommendation", "recommend_restart"]
