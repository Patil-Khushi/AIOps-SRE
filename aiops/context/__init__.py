"""Context Engineering Layer — the single source of truth for incident context.

What this is
------------
Every agent used to gather its own evidence. Four of them independently queried
Prometheus, Loki, Jaeger, the CMDB, the on-call schedule and Git, which meant
``oncall.schedule.lookup`` fired four times per incident, ``itsm.cmdb.lookup``
three, and the RCA agent reasoned from a *different* evidence set than the Log
Correlation agent looking at the same failure. Duplicate round-trips, inconsistent
evidence between agents, no shared ranking, no shared redaction, and no token
budgeting anywhere.

This package collects that evidence **once** per incident, into an immutable
``IncidentContext`` every agent consumes.

The pipeline
------------
``ContextBuilder.build(request)`` runs eight stages. Only the first touches I/O;
the rest are pure functions over data structures, which is what makes them
testable without a single mock:

1. **Collect**    — ``collectors/`` fan out over the tool registry (impure)
2. **Normalise**  — provider payloads become ``Observation`` objects
3. **Correlate**  — cross-source agreement and topology relation attached
4. **Rank**       — deterministic, explainable relevance scoring
5. **Enrich**     — ownership, deployment and topology metadata attached
6. **Redact**     — secrets and PII scrubbed before anything reaches a prompt
7. **Budget**     — projected to fit a consumer's token allowance, never silently
8. **Assemble**   — frozen into an ``IncidentContext``

Rules this package holds itself to
----------------------------------
* **It never imports ``agents``.** The dependency arrow is ``demo/ → agents/ →
  aiops/`` and ``tests/test_layering.py`` fails CI if it is ever reversed. That is
  also why agent-specific projection lives in ``agents/<name>/context_adapter.py``
  rather than here: an adapter reproducing RCA's exact prompt strings belongs to
  RCA, not to the platform.
* **All external I/O goes through the tool registry.** No ``httpx``, no ``kubectl``,
  no vendor SDK. A collector wraps a capability; it never reaches past it.
* **Nothing here raises on the incident path.** ``build()`` degrades to a context
  full of ``UNAVAILABLE`` sections, matching ``resilience.guard``,
  ``topology.resolve`` and ``collect_change_context``. A failure must cost evidence,
  not a verdict. The single exception is requesting a denylisted capability, which
  is a programming error rather than a fact about the world.
* **Absent is not empty.** Four different facts collapse into "no observations" —
  see ``SectionStatus``. Consumers branch on status, never on emptiness.

Rollout
-------
Gated by ``AIOPS_CONTEXT_LAYER`` (``off`` / ``shadow`` / ``on``, default ``off``,
read per call — see ``config.py``). While it is ``off``, every agent keeps its
existing retrieval untouched, so this package can land incrementally without
changing a single agent's behaviour.
"""

from __future__ import annotations

from aiops.context.builder import ContextBuilder, ContextRequest, build
from aiops.context.config import (
    ContextMode,
    context_mode,
    enabled,
    shadow_enabled,
)
from aiops.context.correlation import derive_correlation_id
from aiops.context.denylist import ContextDenylistError, is_denied
from aiops.context.models import (
    Observation,
    SectionSpec,
    SectionStatus,
    Source,
    make_observation_id,
)
from aiops.context.pack import (
    ContextSection,
    IncidentContext,
    IncidentIdentity,
    RankedObservation,
    SecurityMetadata,
    SourceProvenance,
    TokenBudget,
)
from aiops.context.tokenizer import PROFILES, estimate_context_tokens

__all__ = [
    "PROFILES",
    "ContextBuilder",
    "ContextDenylistError",
    "ContextMode",
    "ContextRequest",
    "ContextSection",
    "IncidentContext",
    "IncidentIdentity",
    "Observation",
    "RankedObservation",
    "SectionSpec",
    "SectionStatus",
    "SecurityMetadata",
    "Source",
    "SourceProvenance",
    "TokenBudget",
    "build",
    "context_mode",
    "derive_correlation_id",
    "enabled",
    "estimate_context_tokens",
    "is_denied",
    "make_observation_id",
    "shadow_enabled",
]
