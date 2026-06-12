"""Resolution Verifier (PRS-007 companion).

Runs AFTER an RCA fix is applied and BEFORE a ServiceNow ticket is closed:
re-checks the incident against observability (the same signals that detected
it), attaches a proof work note to the ticket, and — on a clean verification —
raises a HITL "close this ticket?" card. On approval the ticket is resolved in
ServiceNow; the existing SNOW watcher then triggers the Knowledge Synthesizer.

Fully additive + decoupled: triggered fire-and-forget at the end of the
fix-apply handler; any failure here never affects the fix-apply response or the
pipeline.
"""

from __future__ import annotations
