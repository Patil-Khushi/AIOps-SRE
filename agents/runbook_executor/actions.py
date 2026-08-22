"""The action registry — the only vocabulary of things a runbook step may do.

This is the §11 chokepoint. A runbook step names an ``action`` string; that string
must resolve to a spec **in this module** before anything is dispatched. There is no
path from a runbook (or an API caller, or an LLM, or a Knowledge Synthesizer
proposal) to an arbitrary command: an unknown action id is a validation failure, not
a passthrough, and every parameter is checked against the spec's schema and against
the scope the runbook declared.

What this module deliberately does NOT do:

- It does not execute anything. Dispatch stays in ``agent.py`` through
  ``aiops.tools.get_registry().call(...)``, so the HITL gate remains the single
  enforcement point (CLAUDE.md #3).
- It does not decide autonomy. ``AutonomyClass`` here is descriptive metadata used by
  ``risk.py`` to *demand* a human; only ``aiops/policy/gate.py`` can grant one.
- It does not shell out, import ``subprocess``, or evaluate strings. The three
  capabilities it maps onto are the same ones RA-004 has always used.

Everything here is pure and deterministic: no I/O, no clock, no registry lookups.
"""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.runbook_executor.library import ExecutableRunbook
from agents.runbook_executor.models import RunbookStep

# The three capabilities RA-004 dispatches through. Defined here so the action
# registry is the one place that knows the mapping; ``agent.py`` re-exports the
# names it has always exposed (tests import them from there).
SIMULATE_CAP = "automation.runbook.simulate"
APPLY_CAP = "automation.runbook.apply"
EXECUTE_CAP = "automation.runbook.execute"


class BlastRadius(StrEnum):
    """How far one invocation of this action can reach when it goes wrong."""

    NONE = "none"  # read-only
    SINGLE_RESOURCE = "single_resource"  # one pod / one flag
    SERVICE = "service"  # every replica of one service
    MULTI_SERVICE = "multi_service"  # a shared datastore, a dependency
    CLUSTER = "cluster"


class AutonomyClass(IntEnum):
    """§14's ladder, as *descriptive* metadata about an action.

    Never an authorization: ``HUMAN_APPROVAL`` means "this needs a human", and the
    platform gate is what actually obtains one. ``BLOCKED`` means the executor
    refuses before the gate is even consulted — a pre-gate refusal can only ever be
    stricter than policy, which is the safe direction.
    """

    OBSERVE = 0  # observation only, no call at all
    READ_ONLY = 1  # read-only automation
    REVERSIBLE = 2  # low-risk reversible remediation
    HUMAN_APPROVAL = 3  # human approval required
    BLOCKED = 4  # not executable by the agent, ever


ParamType = Literal["str", "int", "bool"]


class ParamSpec(BaseModel):
    """Schema for one action parameter. Absent from a step means the default applies."""

    type: ParamType = "str"
    required: bool = False
    allowed: list[Any] = Field(default_factory=list)  # empty = any value of `type`
    min: int | None = None
    max: int | None = None
    description: str = ""


class ActionSpec(BaseModel):
    """One executable action the platform knows how to perform.

    ``mutating`` answers §16's "Mutation: YES/NO". ``disruptive`` is the stricter bit
    that drives gating: an action can mutate without disrupting traffic (annotating a
    pod for drain), and a step is only allowed to declare ``destructive: false`` for a
    non-disruptive action — see :func:`validate_step`.
    """

    action_id: str
    title: str
    mutating: bool
    disruptive: bool
    reverse_action: str | None = None
    # True when the action moves the system TOWARD its declared baseline, so having no
    # reverse is not an irreversibility risk. Clearing an injected fault is the
    # canonical case: its "reverse" would be re-injecting the fault, which
    # automation.fault.clear refuses by design (a fix that broke something would be
    # the worst outcome of a HITL flow). Without this bit such a step scores as
    # "disruptive, no rollback" = HIGH, which reads as dangerous when it is the
    # opposite. Never set this on an action that destroys state (flush_cache) or that
    # moves away from the declared spec (rollback_deployment).
    restores_default: bool = False
    retry_safe: bool  # safe to re-issue after a timeout / transient failure
    blast_radius: BlastRadius
    autonomy: AutonomyClass
    target_kinds: list[str]
    params: dict[str, ParamSpec] = Field(default_factory=dict)
    expected_impact: str = ""

    @property
    def read_only(self) -> bool:
        return not self.mutating


def _spec(
    action_id: str,
    title: str,
    *,
    mutating: bool,
    disruptive: bool = False,
    reverse_action: str | None = None,
    restores_default: bool = False,
    retry_safe: bool,
    blast_radius: BlastRadius,
    autonomy: AutonomyClass,
    target_kinds: list[str],
    params: dict[str, ParamSpec] | None = None,
    expected_impact: str = "",
) -> ActionSpec:
    return ActionSpec(
        action_id=action_id,
        title=title,
        mutating=mutating,
        disruptive=disruptive,
        reverse_action=reverse_action,
        restores_default=restores_default,
        retry_safe=retry_safe,
        blast_radius=blast_radius,
        autonomy=autonomy,
        target_kinds=target_kinds,
        params=params or {},
        expected_impact=expected_impact,
    )


_REPLICAS = ParamSpec(
    type="int", required=False, min=0, max=10, description="Desired replica count."
)
_TIMEOUT = ParamSpec(
    type="int", required=False, min=1, max=600, description="Seconds to wait for readiness."
)

# The vocabulary. Same list the runbook generator documents
# (scripts/generate_runbooks.py) — a step naming anything else is rejected.
ACTION_SPECS: dict[str, ActionSpec] = {
    "healthcheck": _spec(
        "healthcheck",
        "Check workload health",
        mutating=False,
        retry_safe=True,
        blast_radius=BlastRadius.NONE,
        autonomy=AutonomyClass.READ_ONLY,
        target_kinds=["deployment", "statefulset", "service", "pod"],
        params={"timeout_seconds": _TIMEOUT},
        expected_impact="No change — reads readiness and dependency status.",
    ),
    "snapshot_replicas": _spec(
        "snapshot_replicas",
        "Record current replica count",
        mutating=False,
        retry_safe=True,
        blast_radius=BlastRadius.NONE,
        autonomy=AutonomyClass.READ_ONLY,
        target_kinds=["deployment", "statefulset"],
        expected_impact="No change — captures the value a rollback would restore.",
    ),
    "drain": _spec(
        "drain",
        "Stop routing new traffic to the pods",
        mutating=True,
        disruptive=False,
        reverse_action="undrain",
        retry_safe=True,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.REVERSIBLE,
        target_kinds=["deployment", "statefulset", "pod"],
        expected_impact="In-flight requests finish; no new work is routed to the old pods.",
    ),
    "clear_fault": _spec(
        "clear_fault",
        "Clear the injected fault",
        mutating=True,
        disruptive=True,
        reverse_action=None,  # re-injecting is a scenario action, never a remediation
        restores_default=True,
        retry_safe=True,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["fault", "flag"],
        expected_impact="The workload rolls; the fault toggle returns to its default.",
    ),
    "restart_deployment": _spec(
        "restart_deployment",
        "Restart the deployment",
        mutating=True,
        disruptive=True,
        reverse_action="rescale_previous",
        retry_safe=False,  # a second rollout mid-rollout is not the same operation
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["deployment", "statefulset"],
        params={"timeout_seconds": _TIMEOUT},
        expected_impact="Temporary service disruption may occur while pods are replaced.",
    ),
    "scale_deployment": _spec(
        "scale_deployment",
        "Scale the workload",
        mutating=True,
        disruptive=True,
        reverse_action="rescale_previous",
        retry_safe=True,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["deployment", "statefulset"],
        params={"replicas": _REPLICAS},
        expected_impact="Capacity changes; in-flight requests on removed pods are dropped.",
    ),
    "scale_down": _spec(
        "scale_down",
        "Scale the workload down",
        mutating=True,
        disruptive=True,
        reverse_action="rescale_previous",
        retry_safe=True,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["deployment", "statefulset"],
        params={"replicas": _REPLICAS},
        expected_impact="Capacity is reduced; queued work may back up.",
    ),
    "rescale_previous": _spec(
        "rescale_previous",
        "Restore the previous replica count",
        mutating=True,
        disruptive=True,
        reverse_action=None,
        restores_default=True,
        retry_safe=True,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["deployment", "statefulset"],
        params={"replicas": _REPLICAS},
        expected_impact="Restores the replica count captured before the change.",
    ),
    "rollback_deployment": _spec(
        "rollback_deployment",
        "Roll the deployment back one revision",
        mutating=True,
        disruptive=True,
        reverse_action="redeploy_current",
        retry_safe=False,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["deployment", "statefulset"],
        params={
            "revision": ParamSpec(
                type="int", min=0, max=1000, description="Revision to roll back to."
            )
        },
        expected_impact="The previous image/config is restored; pods are replaced.",
    ),
    "redeploy_current": _spec(
        "redeploy_current",
        "Re-apply the current revision",
        mutating=True,
        disruptive=True,
        reverse_action="rollback_deployment",
        restores_default=True,
        retry_safe=False,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["deployment", "statefulset"],
        expected_impact="Pods are replaced with the currently declared spec.",
    ),
    "flush_cache": _spec(
        "flush_cache",
        "Flush the cache",
        mutating=True,
        disruptive=True,
        reverse_action=None,  # cached data cannot be un-flushed
        retry_safe=True,
        blast_radius=BlastRadius.MULTI_SERVICE,
        autonomy=AutonomyClass.HUMAN_APPROVAL,
        target_kinds=["deployment", "statefulset", "service"],
        expected_impact="Cache is emptied; a latency spike is expected while it refills.",
    ),
    "undrain": _spec(
        "undrain",
        "Resume routing traffic to the pods",
        mutating=True,
        disruptive=False,
        reverse_action="drain",
        restores_default=True,
        retry_safe=True,
        blast_radius=BlastRadius.SERVICE,
        autonomy=AutonomyClass.REVERSIBLE,
        target_kinds=["deployment", "statefulset", "pod"],
        expected_impact="Traffic returns to the pods.",
    ),
}


# ── target parsing + scope ───────────────────────────────────────────────────

# Kubernetes object names: lowercase alphanumeric, '-' and '.' inside.
_K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")
# Failure keys as the injection registry spells them: user_service.mysql_down
_FAULT_KEY_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*\.[a-z0-9]+(_[a-z0-9]+)*$")

# Substrings that must never appear in a target, namespace or string parameter.
# The threat is not a clever exploit: it is an alert annotation, an LLM summary or an
# API override flowing into a value that a *future* provider passes to a shell. The
# executor refuses the value at the boundary so no provider has to be trusted.
_INJECTION_MARKERS: tuple[str, ...] = (
    ";",
    "|",
    "&",
    "$",
    "`",
    chr(92),  # backslash
    "\n",
    "\r",
    "\t",
    "<",
    ">",
    "*",
    "?",
    "..",
    "kubectl",
    "python",
    "bash",
    "sh -c",
    "eval",
    "exec",
    "import ",
    "rm ",
    "curl",
    "wget",
    "http://",
    "https://",
)


class TargetRef(BaseModel):
    """A parsed step target: ``<kind>/<name>``."""

    raw: str
    kind: str
    name: str
    # The service this target belongs to, normalized for comparison against the
    # runbook's declared scope. For a fault key that is the half before the dot.
    service: str


def _normalize_service(service: str | None) -> str:
    """Collapse spellings to a base key — mirrors ``selector._normalize_service``
    and ``aiops.runbooks.store._normalize_service`` (duplicated deliberately: this
    module must not depend on the platform seam)."""
    s = (service or "").lower().strip()
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


def contains_injection(value: str) -> str:
    """The first refusal marker found in ``value``, or ``""`` when it is clean."""
    lowered = (value or "").lower()
    for marker in _INJECTION_MARKERS:
        if marker in lowered:
            return marker
    return ""


def parse_target(raw: str) -> tuple[TargetRef | None, str]:
    """Parse ``<kind>/<name>`` into a :class:`TargetRef`, or return why it is invalid.

    Returns ``(ref, "")`` on success and ``(None, reason)`` on failure — no
    exceptions, because every caller is on a validation path that wants to collect
    all the reasons rather than stop at the first.
    """
    text = (raw or "").strip()
    if not text:
        return None, "target is empty"
    marker = contains_injection(text)
    if marker:
        return None, f"target {text!r} contains a refused character/keyword ({marker!r})"
    kind, sep, name = text.partition("/")
    if not sep or not name:
        return None, f"target {text!r} must be '<kind>/<name>' (e.g. 'deployment/order-service')"
    kind, name = kind.strip().lower(), name.strip()
    if kind in ("fault", "flag"):
        if not _FAULT_KEY_RE.match(name):
            return None, f"fault key {name!r} is not a '<service>.<failure>' key"
        return TargetRef(raw=text, kind=kind, name=name, service=name.split(".", 1)[0]), ""
    if not _K8S_NAME_RE.match(name):
        return None, f"resource name {name!r} is not a valid Kubernetes object name"
    return TargetRef(raw=text, kind=kind, name=name, service=name), ""


def allowed_services(runbook: ExecutableRunbook) -> set[str]:
    """Normalized service keys this runbook is allowed to act on.

    Falls back to the runbook's own service when the declaration is missing, so an
    absent allow-list is the *narrowest* scope rather than the widest (§12).
    """
    declared = runbook.applicability.allowed_services or [runbook.service]
    return {_normalize_service(s) for s in declared if s} | {_normalize_service(runbook.service)}


def target_in_scope(step: RunbookStep, runbook: ExecutableRunbook) -> tuple[bool, str]:
    """Is this step's target inside the runbook's declared service/namespace scope?"""
    ref, reason = parse_target(step.target or "")
    if ref is None:
        return False, reason
    permitted = allowed_services(runbook)
    if _normalize_service(ref.service) not in permitted:
        return False, (
            f"step {step.name!r} targets {ref.raw!r} (service {ref.service!r}), which is "
            f"outside this runbook's declared scope {sorted(permitted)}"
        )
    namespaces = runbook.applicability.allowed_namespaces
    if namespaces and step.namespace not in namespaces:
        return False, (
            f"step {step.name!r} targets namespace {step.namespace!r}, which is outside "
            f"the runbook's declared namespaces {sorted(namespaces)}"
        )
    return True, ""


# ── resolution + validation ──────────────────────────────────────────────────


class StepValidation(BaseModel):
    """Outcome of validating one step against the registry and the runbook scope."""

    step_name: str
    action_id: str
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    spec: ActionSpec | None = None
    target: TargetRef | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)


def resolve_action(action_id: str) -> ActionSpec | None:
    """The spec for ``action_id``, or ``None`` when it is not in the registry."""
    return ACTION_SPECS.get((action_id or "").strip())


def capability_for(step: RunbookStep) -> str:
    """Which capability this step dispatches through.

    Unchanged from RA-004 v0 on purpose: the ``destructive`` bit decides, so the
    REQUIRED-HITL path is exactly the same one the existing tests exercise. The
    action registry *constrains* what may declare itself non-destructive (see
    :func:`validate_step`); it does not re-route.
    """
    return EXECUTE_CAP if step.destructive else APPLY_CAP


def _validate_params(
    spec: ActionSpec, params: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Type/range/allow-list check every parameter against the spec's schema."""
    errors: list[str] = []
    warnings: list[str] = []
    clean: dict[str, Any] = {}
    for key, value in (params or {}).items():
        pspec = spec.params.get(key)
        if pspec is None:
            errors.append(
                f"parameter {key!r} is not declared by action {spec.action_id!r} "
                f"(declared: {sorted(spec.params) or 'none'})"
            )
            continue
        if pspec.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"parameter {key!r} must be an int, got {type(value).__name__}")
                continue
            if pspec.min is not None and value < pspec.min:
                errors.append(f"parameter {key!r}={value} is below the minimum {pspec.min}")
                continue
            if pspec.max is not None and value > pspec.max:
                errors.append(f"parameter {key!r}={value} is above the maximum {pspec.max}")
                continue
        elif pspec.type == "bool":
            if not isinstance(value, bool):
                errors.append(f"parameter {key!r} must be a bool, got {type(value).__name__}")
                continue
        else:  # str
            if not isinstance(value, str):
                errors.append(f"parameter {key!r} must be a str, got {type(value).__name__}")
                continue
            marker = contains_injection(value)
            if marker:
                errors.append(
                    f"parameter {key!r} contains a refused character/keyword ({marker!r})"
                )
                continue
        if pspec.allowed and value not in pspec.allowed:
            errors.append(f"parameter {key!r}={value!r} is not one of {pspec.allowed}")
            continue
        clean[key] = value
    for key, pspec in spec.params.items():
        if pspec.required and key not in clean:
            errors.append(f"action {spec.action_id!r} requires parameter {key!r}")
    return clean, errors, warnings


def validate_step(
    step: RunbookStep,
    runbook: ExecutableRunbook,
    *,
    overrides: dict[str, Any] | None = None,
) -> StepValidation:
    """Resolve and fully validate one step. Never raises.

    Refuses, in order: an unknown action id; a target that is malformed, injection-
    shaped, of a kind the action does not accept, or outside the runbook's declared
    scope; a namespace that fails the same checks; a disruptive action that declares
    itself non-destructive (which would route it around the HITL gate); a rollback
    action that is not itself in the registry; and any parameter that is undeclared,
    mistyped, out of range, or injection-shaped.
    """
    action_id = (step.action or "").strip()
    spec = resolve_action(action_id)
    if spec is None:
        return StepValidation(
            step_name=step.name,
            action_id=action_id,
            ok=False,
            errors=[
                f"step {step.name!r}: unknown action {action_id!r} — not in the action "
                f"registry ({len(ACTION_SPECS)} known actions)"
            ],
        )

    errors: list[str] = []
    warnings: list[str] = []

    ref, reason = parse_target(step.target or "")
    if ref is None:
        errors.append(f"step {step.name!r}: {reason}")
    else:
        if ref.kind not in spec.target_kinds:
            errors.append(
                f"step {step.name!r}: action {action_id!r} cannot target a {ref.kind!r} "
                f"(accepts: {', '.join(spec.target_kinds)})"
            )
        in_scope, scope_reason = target_in_scope(step, runbook)
        if not in_scope:
            errors.append(scope_reason)

    ns_marker = contains_injection(step.namespace or "")
    if ns_marker:
        errors.append(
            f"step {step.name!r}: namespace {step.namespace!r} contains a refused "
            f"character/keyword ({ns_marker!r})"
        )
    elif step.namespace and not _K8S_NAME_RE.match(step.namespace):
        errors.append(f"step {step.name!r}: namespace {step.namespace!r} is not a valid name")

    if spec.disruptive and not step.destructive:
        errors.append(
            f"step {step.name!r}: action {action_id!r} is disruptive but the step declares "
            "destructive: false, which would route it around the HITL gate"
        )
    if step.destructive and not spec.mutating:
        warnings.append(
            f"step {step.name!r}: read-only action {action_id!r} is marked destructive — "
            "it will ask a human to approve a read"
        )
    if step.rollback_action:
        reverse = resolve_action(step.rollback_action)
        if reverse is None:
            errors.append(
                f"step {step.name!r}: rollback_action {step.rollback_action!r} is not in "
                "the action registry"
            )
        elif reverse.disruptive and not step.destructive:
            # Undoing this costs more than doing it. The executor gates such a reverse on
            # its own merits (see agent._rollback), so this is a warning rather than a
            # refusal — but the operator approving the forward step should know that its
            # rollback will stop to ask them again.
            warnings.append(
                f"step {step.name!r}: non-destructive step declares the disruptive "
                f"reverse {step.rollback_action!r} ({reverse.blast_radius.value} blast "
                "radius) — the rollback will require its own approval"
            )
        elif reverse.reverse_action is None and not reverse.restores_default:
            warnings.append(
                f"step {step.name!r}: rollback {step.rollback_action!r} is itself "
                "irreversible — a failed rollback cannot be undone"
            )
    elif spec.disruptive and spec.reverse_action:
        warnings.append(
            f"step {step.name!r}: {action_id!r} has a known reverse "
            f"({spec.reverse_action!r}) but the step declares no rollback_action"
        )

    merged: dict[str, Any] = dict(step.params or {})
    merged.update(overrides or {})
    clean, perrors, pwarnings = _validate_params(spec, merged)
    errors += [f"step {step.name!r}: {e}" for e in perrors]
    warnings += [f"step {step.name!r}: {w}" for w in pwarnings]

    return StepValidation(
        step_name=step.name,
        action_id=action_id,
        ok=not errors,
        errors=errors,
        warnings=warnings,
        spec=spec,
        target=ref,
        parameters=clean,
    )


def validate_runbook(
    runbook: ExecutableRunbook,
    *,
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[StepValidation]:
    """Validate every step, in order. ``overrides`` is keyed by step name."""
    per_step = overrides or {}
    return [validate_step(s, runbook, overrides=per_step.get(s.name)) for s in runbook.steps]


__all__ = [
    "ACTION_SPECS",
    "APPLY_CAP",
    "EXECUTE_CAP",
    "SIMULATE_CAP",
    "ActionSpec",
    "AutonomyClass",
    "BlastRadius",
    "ParamSpec",
    "StepValidation",
    "TargetRef",
    "allowed_services",
    "capability_for",
    "contains_injection",
    "parse_target",
    "resolve_action",
    "target_in_scope",
    "validate_runbook",
    "validate_step",
]
