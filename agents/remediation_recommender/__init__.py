"""Remediation Recommender (PRS-001).

Public surface — import from here, not from internal modules::

    from agents.remediation_recommender import (
        recommend,
        run,
        RemediationInput,
        RemediationVerdict,
        RemediationOption,
        BlastRadius,
        ActionType,
    )
"""

from .agent import recommend, run
from .models import (
    ActionType,
    BlastRadius,
    Environment,
    OptionSource,
    RecoAuditMetadata,
    RemediationInput,
    RemediationOption,
    RemediationVerdict,
)

__all__ = [
    "ActionType",
    "BlastRadius",
    "Environment",
    "OptionSource",
    "RecoAuditMetadata",
    "RemediationInput",
    "RemediationOption",
    "RemediationVerdict",
    "recommend",
    "run",
]
