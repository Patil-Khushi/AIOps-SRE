"""Provider interface for topology (service-dependency) discovery.

A *topology provider* answers one question: "which services does ``service``
depend on?" Providers are ranked into a chain (see ``resolver.py``) and the
first one that resolves a non-empty dependency list wins.

Why a chain instead of the tool registry's provider selection
-------------------------------------------------------------
``aiops.tools.registry`` binds **one** active provider per capability
(``_active.setdefault(capability, name)`` — first registration wins, and the
winner is import-order dependent). That is the right model for "create a
ticket": you want exactly one ITSM system. It cannot express "try OTel, then
the CMDB, then fall back to the static table", which is what topology needs —
each source has different coverage and different failure modes.

So providers here are *not* competing registrations under one capability. Each
owns a distinct capability name and the resolver walks them in order. This also
means the existing ``itsm.cmdb.dependencies`` capability — consumed by
``alert_triage``, ``notification_assembler`` **and** ``log_correlation`` — is
left completely untouched: adding topology providers cannot change which
provider those three agents get.

The four outcomes are distinct on purpose
-----------------------------------------
``ProviderStatus`` separates "I asked and the answer is genuinely nothing" from
"I could not ask". Both fall through to the next tier, but only ``FAILED``
counts as an error worth tripping a circuit breaker or logging as a warning.
On a stock ServiceNow PDI the demo services have no CI records at all, so
``EMPTY`` is the *normal* result for that tier, not an edge case — treating it
as a failure would spam warnings and open breakers during healthy operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ProviderStatus(StrEnum):
    """Outcome of a single provider lookup.

    ``StrEnum`` so the value serializes directly into a decision trace or
    ``ToolResult.metadata`` without a cast (repo targets Python 3.12).
    """

    RESOLVED = "resolved"
    """Queried successfully and found at least one dependency."""

    EMPTY = "empty"
    """Queried successfully; the source genuinely has no relationships for this
    service. A legitimate answer — falls through to the next tier without
    being counted as an error."""

    UNAVAILABLE = "unavailable"
    """Could not query: capability not registered, credentials absent, or the
    provider is disabled. A clean skip — not a failure, because a provider that
    was never configured has not malfunctioned."""

    FAILED = "failed"
    """The query was attempted and errored (timeout, HTTP error, bad payload).
    The only status that trips the circuit breaker."""


@dataclass(frozen=True)
class TopologyResult:
    """One provider's answer, plus enough provenance to explain the chain.

    ``dependencies`` is always a list — never ``None`` — so callers can treat
    every status uniformly and only branch when they care about *why* a list is
    empty.
    """

    provider: str
    status: ProviderStatus
    dependencies: list[str] = field(default_factory=list)
    error: str | None = None
    note: str | None = None
    latency_ms: float = 0.0
    cached: bool = False
    payload_present: bool = False
    """Whether the source returned a body at all, independent of whether that
    body listed any dependencies.

    Needed because "the CMDB has a record for this service and it has no
    dependencies" and "the CMDB returned nothing" are different facts that
    RA-007's decision trace has always reported with different wording. Folding
    them into a single ``EMPTY`` status would silently reword an operator-facing
    audit line."""

    @property
    def resolved(self) -> bool:
        """True when this result should stop the chain."""
        return self.status is ProviderStatus.RESOLVED and bool(self.dependencies)


@dataclass(frozen=True)
class HealthStatus:
    """Cheap liveness answer for a provider, cached by the resolver.

    Deliberately not a bool: ``detail`` is what makes an unhealthy provider
    diagnosable from a decision trace without re-running the lookup.
    """

    healthy: bool
    detail: str = ""


@runtime_checkable
class TopologyProvider(Protocol):
    """What the resolver requires of a provider.

    A ``Protocol`` rather than an ABC so providers stay plain modules/objects
    with no inheritance coupling to this package — the same
    wrap-the-dependency-behind-a-thin-interface posture the tool registry takes
    (CLAUDE.md principle #1).
    """

    name: str

    def health(self) -> HealthStatus:
        """Report whether a lookup is worth attempting. Must not raise."""
        ...

    def resolve(self, service: str, *, timeout_s: float) -> TopologyResult:
        """Look up ``service``'s dependencies. Must not raise — every failure
        mode is expressed as a ``TopologyResult`` status so the resolver never
        needs a bare ``except`` around provider code."""
        ...
