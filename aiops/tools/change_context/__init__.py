"""Deployment and configuration change context — evidence only.

Answers "what changed around this incident?" across seven sources: GitHub, GitLab,
ArgoCD, Jenkins, feature flags, Kubernetes rollout, and configuration changes.

It infers no causality and names no root cause. Records are facts — this shipped,
this flag is on, at this time, by this person — sorted chronologically rather than
by suspicion, because time is a fact and blame is a judgement the RCA agent makes.

A union across providers, not a fallback chain: a commit and a flag flip can both
be true, so stopping at the first answer would discard most of the picture.
"""

from aiops.tools.change_context.base import (
    ChangeContext,
    ChangeContextProvider,
    ChangeContextResult,
    ChangeRecord,
    ChangeType,
    ProviderStatus,
    RollbackStatus,
)
from aiops.tools.change_context.collector import (
    collect_change_context,
    register_provider,
)

__all__ = [
    "ChangeContext",
    "ChangeContextProvider",
    "ChangeContextResult",
    "ChangeRecord",
    "ChangeType",
    "ProviderStatus",
    "RollbackStatus",
    "collect_change_context",
    "register_provider",
]
