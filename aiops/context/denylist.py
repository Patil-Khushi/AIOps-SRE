"""Capabilities the Context Engineering Layer refuses to serve.

The layer caches, retries and shares whatever it collects. That is exactly right
for a read of telemetry and exactly wrong for two kinds of capability, so they are
refused at the boundary rather than handled carefully everywhere.

``automation.fault.clear`` — the one that would break something
---------------------------------------------------------------
``agents/rca_agent/agent.py`` calls this capability twice with ``fault=""`` and
``fault="__probe__"`` purely to read ``metadata["available_faults"]``. It is not
remediation; it is how the RCA agent discovers which fault names actually exist so
it can ground an LLM's proposed fix against reality instead of inventing a flag.
``aiops/policy/gate.py`` documents that it must stay ``AutonomyLevel.NONE``
*because* RCA probes it on every pass.

Route that probe through this layer and a single cached failure disables grounding
for the whole TTL window — during which the RCA agent stops correcting the model
and the dashboard starts offering one-click fixes for flags that do not exist.
Nothing would fail; the system would just get quietly less correct. That is the
worst possible failure mode, so it is refused structurally.

``feature_flags.*`` — the one that no longer exists
---------------------------------------------------
The flagd seam was removed with the OpenTelemetry demo. No provider registers
these capabilities any more, so wrapping them would manufacture a permanently
``UNAVAILABLE`` section that looks like a misconfiguration rather than a deleted
feature. (``aiops/tools/change_context/providers.py`` still probes
``feature_flags.list_variants`` and reports unavailable forever — a known orphan,
deliberately left alone.)

Mutations generally
-------------------
Nothing that changes the world belongs here. Creating a ticket, sending a
notification, executing a runbook — those are decisions an agent makes once, not
evidence to be shared and replayed from a cache. Deduplicating a ticket creation
would be a bug, not an optimisation.

Why this raises
---------------
Every other failure in this package degrades to a status on a result object,
because a backend being down must cost evidence rather than a verdict. This is the
single exception: asking the context layer to fetch a denied capability is a
*programming* error, not a fact about the world, and it can only be fixed by
changing the calling code. Failing loudly at request-construction time — before any
I/O — is the only way the mistake gets noticed at all.
"""

from __future__ import annotations

DENIED_CAPABILITIES: frozenset[str] = frozenset(
    {
        # RCA's flag-name grounding probe — see the module docstring. Do not add a
        # collector for this even though it is technically a read.
        "automation.fault.clear",
        # Mutations. Listed explicitly rather than pattern-matched so adding a
        # capability to the registry cannot accidentally become collectable.
        "automation.runbook.execute",
        "automation.runbook.simulate",
        "automation.runbook.apply",
        "itsm.incident.create",
        "itsm.incident.update",
        "itsm.incident.attachment.add",
        "itsm.ticket.close",
        "knowledge.publish",
        "notify.send",
        "chatops.war_room.create",
        "rca.fix_step.execute",
    }
)

DENIED_PREFIXES: tuple[str, ...] = (
    # Seam deleted with the OTel demo; no provider serves these.
    "feature_flags.",
)


class ContextDenylistError(RuntimeError):
    """Raised when a caller asks the context layer for a denied capability.

    Carries ``capability`` so a caller that catches this can report *which*
    capability it asked for without parsing the message.
    """

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(
            f"aiops.context refuses to collect {capability!r}: {reason}. "
            "Call the capability directly through aiops.tools.get_registry() if the "
            "caller genuinely needs it."
        )
        self.capability = capability
        self.reason = reason


def denial_reason(capability: str) -> str | None:
    """Why ``capability`` is refused, or ``None`` if it is allowed."""
    if capability in DENIED_CAPABILITIES:
        return "it is a mutation or a grounding probe, not shareable evidence"
    for prefix in DENIED_PREFIXES:
        if capability.startswith(prefix):
            return f"the {prefix}* seam has no registered provider"
    return None


def is_denied(capability: str) -> bool:
    return denial_reason(capability) is not None


def ensure_allowed(capability: str) -> None:
    """Raise ``ContextDenylistError`` if ``capability`` must not be collected."""
    reason = denial_reason(capability)
    if reason is not None:
        raise ContextDenylistError(capability, reason)
