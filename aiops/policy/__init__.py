"""Platform-enforced HITL gate.

Solution Design slide 10: every action is None / Optional / Required HITL.
Required-HITL actions cannot be bypassed by a buggy or compromised agent — the
gate is checked before tool invocation, on the platform side.

Phase 0 ships an in-process gate with hard-coded defaults. Phase 1+ wires this
to OPA so the rules in ``policies/hitl.rego`` are the source of truth.
"""

from .approvals import (
    ApprovalError,
    ApprovalRegistry,
    ApprovalRequest,
    ApprovalRequester,
    ApprovalStatus,
    get_approval_registry,
    install_chatops_listener,
    install_default_approver,
)
from .gate import (
    ApprovalSummary,
    ApproverResult,
    AutonomyLevel,
    Decision,
    GateError,
    HITLGate,
    get_gate,
)

__all__ = [
    "ApprovalError",
    "ApprovalRegistry",
    "ApprovalRequest",
    "ApprovalRequester",
    "ApprovalStatus",
    "ApprovalSummary",
    "ApproverResult",
    "AutonomyLevel",
    "Decision",
    "GateError",
    "HITLGate",
    "get_approval_registry",
    "get_gate",
    "install_chatops_listener",
    "install_default_approver",
]
