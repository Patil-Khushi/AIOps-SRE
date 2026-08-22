"""Applicability + prerequisite evaluation — "may this runbook run on this incident?"

Deterministic and pure: same incident + same runbook ⇒ same verdict, no I/O, no LLM,
no clock except the ``now`` a caller passes in. Everything a consumer needs to explain
the verdict comes back on :class:`ApplicabilityResult` — per-facet verdicts,
per-prerequisite results, positive reasons, blocking reasons and warnings — so the
matcher can score it (``matching.py``), the dry run can block on it (``dryrun.py``) and
the UI can render it, all from one evaluation.

Two distinctions this module is careful about:

**Mismatch is not the same as unknown.** An incident that never told us its environment
has not *failed* the environment check — it is unverified. Unknown facets warn; only a
genuine contradiction makes a runbook NOT_APPLICABLE. Failing closed on unknowns would
make every runbook inapplicable off-cluster, which is not safety, it is paralysis.

**Disqualifying facets are a closed, named set.** ``_DISQUALIFYING`` lists the facets
where a contradiction means "this is the wrong procedure". Signals and tags are
deliberately *not* in it: they are sparse, keyword-derived, and a missing one means "we
did not observe it", not "the incident is not that". They cost score and raise a
warning instead.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from agents.runbook_executor import actions
from agents.runbook_executor.library import ExecutableRunbook
from agents.runbook_executor.models import Incident, Prerequisite

# Incident lifecycle states that mean "still worth acting on".
#
# ``suppressed`` is in here on purpose: in this codebase a Suppressed verdict is a
# *deduplicated* alert (Alert Triage clustered it onto an existing incident), not a
# resolved one. Treating it as inactive would block remediation for every duplicate of
# a live incident — the same trap ``save_rca_result_for_cluster`` exists to avoid.
_ACTIVE_STATES = frozenset({"active", "open", "new", "in_progress", "firing", "suppressed", "ack"})
_INACTIVE_STATES = frozenset(
    {"resolved", "closed", "cancelled", "canceled", "superseded", "duplicate_closed", "expired"}
)

_DEFAULT_MAX_AGE_MINUTES = 240


class ApplicabilityStatus(StrEnum):
    """Whether this runbook may be run for this incident.

    ``NOT_APPLICABLE`` — a declared facet contradicts the incident: wrong procedure.
    ``BLOCKED``        — the right procedure, refused: not ACTIVE/approved, a mandatory
                         prerequisite failed, the incident is stale, a step is out of
                         scope. §6 CASE 5.
    ``UNKNOWN``        — a mandatory prerequisite could not be evaluated. Never
                         executes; feeds AMBIGUOUS upstream.
    """

    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class FacetVerdict(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"  # the incident did not supply this field
    UNCONSTRAINED = "unconstrained"  # the runbook declares nothing here


class PrerequisiteStatus(StrEnum):
    SATISFIED = "satisfied"
    FAILED = "failed"
    UNKNOWN = "unknown"  # no evaluator for this check
    SKIPPED = "skipped"  # evaluable in principle, but the data source was unavailable


# Facets where a MISMATCH disqualifies the runbook outright.
_DISQUALIFYING = (
    "service",
    "environment",
    "failure_category",
    "alert",
    "incident_type",
    "severity",
)


class FacetResult(BaseModel):
    name: str
    verdict: FacetVerdict
    detail: str = ""


class PrerequisiteResult(BaseModel):
    id: str
    description: str = ""
    mandatory: bool = True
    check: str = "manual"
    status: PrerequisiteStatus = PrerequisiteStatus.UNKNOWN
    detail: str = ""


class IncidentContext(BaseModel):
    """Everything the executor was *told* about the incident.

    A superset of :class:`~agents.runbook_executor.models.Incident` — that model stays
    the RA-002 hand-off shape and the legacy entry points keep using it, while this
    carries the extra facets production matching needs. Every added field is optional,
    and an omitted field reads as "unknown" rather than as a mismatch, so the legacy
    ``execute_runbook(Incident(...))`` path behaves exactly as it did.
    """

    incident_id: str = ""
    service: str
    environment: str = ""
    severity: str | None = None
    alert_name: str = ""
    failure_category: str = ""
    incident_type: str = ""  # RA-002 classification: application / infrastructure / …
    tags: list[str] = Field(default_factory=list)
    observed_signals: list[str] = Field(default_factory=list)
    summary: str = ""
    # Lifecycle. ``incident_status=""`` means "not supplied" — see _ACTIVE_STATES.
    incident_status: str = ""
    detected_at: datetime | None = None
    # Tri-state: True = confirmed firing, False = confirmed not firing, None = not probed.
    alert_firing: bool | None = None
    max_incident_age_minutes: int | None = None
    requested_by: str = ""

    @classmethod
    def from_incident(cls, incident: Incident, **extra: object) -> IncidentContext:
        """Widen the legacy :class:`Incident` hand-off into a full context."""
        base: dict[str, object] = {
            "incident_id": incident.incident_id,
            "service": incident.service,
            "severity": incident.severity,
            "tags": list(incident.tags),
        }
        base.update({k: v for k, v in extra.items() if v is not None})
        return cls.model_validate(base)

    def to_incident(self) -> Incident:
        """Narrow back to the RA-002 hand-off shape the execution core consumes."""
        return Incident(
            incident_id=self.incident_id,
            service=self.service,
            severity=self.severity,
            tags=list(self.tags),
        )


class ApplicabilityResult(BaseModel):
    """Why this runbook is (or is not) applicable to this incident."""

    runbook_id: str
    runbook_version: int
    status: ApplicabilityStatus
    reasons: list[str] = Field(default_factory=list)  # positive — feeds match_reasons
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    facets: list[FacetResult] = Field(default_factory=list)
    prerequisites: list[PrerequisiteResult] = Field(default_factory=list)

    @property
    def applicable(self) -> bool:
        return self.status is ApplicabilityStatus.APPLICABLE

    @property
    def missing_prerequisites(self) -> list[str]:
        """Prerequisite ids that are not satisfied — the §4 candidate field."""
        return [p.id for p in self.prerequisites if p.status is not PrerequisiteStatus.SATISFIED]

    def facet(self, name: str) -> FacetResult | None:
        return next((f for f in self.facets if f.name == name), None)


# ── helpers ──────────────────────────────────────────────────────────────────


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _norm_token(value: str | None) -> str:
    """Lower-case and strip separators so 'Sev-1' / 'sev_1' / 'sev 1' compare equal."""
    token = _norm(value)
    for sep in ("-", "_", " "):
        token = token.replace(sep, "")
    return token


def _max_age_minutes(ctx: IncidentContext) -> int:
    """Per-call env read (never at import — that defeats monkeypatch in tests)."""
    if ctx.max_incident_age_minutes is not None:
        return max(0, int(ctx.max_incident_age_minutes))
    raw = os.environ.get("AIOPS_RUNBOOK_MAX_INCIDENT_AGE_MINUTES", "").strip()
    try:
        return max(0, int(raw)) if raw else _DEFAULT_MAX_AGE_MINUTES
    except ValueError:
        return _DEFAULT_MAX_AGE_MINUTES


def _list_facet(name: str, declared: list[str], observed: str, *, label: str) -> FacetResult:
    """Verdict for a facet whose declaration is a list of acceptable values."""
    if not declared:
        return FacetResult(name=name, verdict=FacetVerdict.UNCONSTRAINED, detail=f"any {label}")
    if not observed:
        return FacetResult(
            name=name,
            verdict=FacetVerdict.UNKNOWN,
            detail=f"incident carries no {label}; declared {declared}",
        )
    wanted = {_norm_token(d) for d in declared}
    if _norm_token(observed) in wanted:
        return FacetResult(
            name=name, verdict=FacetVerdict.MATCH, detail=f"{label} {observed!r} matches"
        )
    return FacetResult(
        name=name,
        verdict=FacetVerdict.MISMATCH,
        detail=f"{label} {observed!r} is not one of {declared}",
    )


def service_matches(runbook_service: str, incident_service: str) -> bool:
    """Same substring-either-direction rule the v0 selector used, so a service
    spelling that matched before still matches (``payment`` ↔ ``payment-service``)."""
    a, b = actions._normalize_service(runbook_service), actions._normalize_service(incident_service)
    if not a or not b:
        return False
    return a in b or b in a


def evaluate_facets(runbook: ExecutableRunbook, ctx: IncidentContext) -> list[FacetResult]:
    """Per-facet verdicts. Consumed by both the matcher (score) and the UI (why)."""
    scope = runbook.applicability
    facets: list[FacetResult] = []

    if service_matches(runbook.service, ctx.service):
        facets.append(
            FacetResult(
                name="service",
                verdict=FacetVerdict.MATCH,
                detail=f"runbook service {runbook.service!r} matches {ctx.service!r}",
            )
        )
    else:
        facets.append(
            FacetResult(
                name="service",
                verdict=FacetVerdict.MISMATCH,
                detail=f"runbook is for {runbook.service!r}, incident is on {ctx.service!r}",
            )
        )

    facets.append(
        _list_facet("environment", scope.environments, ctx.environment, label="environment")
    )
    facets.append(
        _list_facet(
            "failure_category",
            [scope.failure_category] if scope.failure_category else [],
            ctx.failure_category,
            label="failure category",
        )
    )
    facets.append(_list_facet("alert", scope.alerts, ctx.alert_name, label="alert"))
    facets.append(
        _list_facet("incident_type", scope.incident_types, ctx.incident_type, label="incident type")
    )
    facets.append(_list_facet("severity", scope.severities, ctx.severity or "", label="severity"))

    # Signals: advisory. All present is a strong positive; a missing one is "not
    # observed", which costs score and warns but never disqualifies.
    if not scope.required_signals:
        facets.append(
            FacetResult(
                name="required_signals", verdict=FacetVerdict.UNCONSTRAINED, detail="none required"
            )
        )
    elif not ctx.observed_signals:
        facets.append(
            FacetResult(
                name="required_signals",
                verdict=FacetVerdict.UNKNOWN,
                detail=f"no signals observed; runbook expects {scope.required_signals}",
            )
        )
    else:
        observed = {_norm(s) for s in ctx.observed_signals}
        missing = [s for s in scope.required_signals if _norm(s) not in observed]
        if missing:
            facets.append(
                FacetResult(
                    name="required_signals",
                    verdict=FacetVerdict.MISMATCH,
                    detail=f"expected signal(s) not observed: {missing}",
                )
            )
        else:
            facets.append(
                FacetResult(
                    name="required_signals",
                    verdict=FacetVerdict.MATCH,
                    detail=f"all required signals present: {scope.required_signals}",
                )
            )

    # Tags: pure overlap, always advisory. Kept as a facet so the score has one
    # source of truth for every input it uses.
    rb_tags = {_norm(t) for t in runbook.tags}
    want = {_norm(t) for t in ctx.tags}
    overlap = sorted(w for w in want if w and any(w in t or t in w for t in rb_tags))
    if not rb_tags or not want:
        facets.append(
            FacetResult(name="tags", verdict=FacetVerdict.UNKNOWN, detail="no tags to compare")
        )
    elif overlap:
        facets.append(
            FacetResult(
                name="tags", verdict=FacetVerdict.MATCH, detail=f"symptom tags matched: {overlap}"
            )
        )
    else:
        facets.append(
            FacetResult(name="tags", verdict=FacetVerdict.MISMATCH, detail="no symptom tag overlap")
        )
    return facets


# ── prerequisite checks ──────────────────────────────────────────────────────


def _check_incident_active(
    ctx: IncidentContext, *, now: datetime
) -> tuple[PrerequisiteStatus, str]:
    state = _norm(ctx.incident_status)
    if state in _INACTIVE_STATES:
        return (
            PrerequisiteStatus.FAILED,
            f"incident status is {ctx.incident_status!r} — remediation is not applied to a "
            "closed incident",
        )
    if ctx.detected_at is not None:
        detected = (
            ctx.detected_at if ctx.detected_at.tzinfo else ctx.detected_at.replace(tzinfo=UTC)
        )
        age_minutes = (now - detected).total_seconds() / 60.0
        limit = _max_age_minutes(ctx)
        if age_minutes > limit:
            return (
                PrerequisiteStatus.FAILED,
                f"incident is {age_minutes:.0f} min old, past the {limit} min limit — "
                "re-triage before remediating",
            )
        if state in _ACTIVE_STATES:
            return (
                PrerequisiteStatus.SATISFIED,
                f"incident is {state} and {age_minutes:.0f} min old",
            )
        return (
            PrerequisiteStatus.UNKNOWN,
            f"incident status not supplied; age {age_minutes:.0f} min is within the limit",
        )
    if state in _ACTIVE_STATES:
        return PrerequisiteStatus.SATISFIED, f"incident is {state}"
    return PrerequisiteStatus.UNKNOWN, "incident status and detection time were not supplied"


def _check_service_scope(runbook: ExecutableRunbook) -> tuple[PrerequisiteStatus, str]:
    problems = [
        reason
        for step in runbook.steps
        for ok, reason in [actions.target_in_scope(step, runbook)]
        if not ok
    ]
    if problems:
        return PrerequisiteStatus.FAILED, "; ".join(problems)
    return (
        PrerequisiteStatus.SATISFIED,
        f"all {len(runbook.steps)} step target(s) inside the declared scope",
    )


def _check_alert_firing(ctx: IncidentContext) -> tuple[PrerequisiteStatus, str]:
    if ctx.alert_firing is True:
        return PrerequisiteStatus.SATISFIED, f"{ctx.alert_name or 'the alert'} is still firing"
    if ctx.alert_firing is False:
        return (
            PrerequisiteStatus.FAILED,
            f"{ctx.alert_name or 'the alert'} is no longer firing — the condition may have "
            "cleared on its own",
        )
    return PrerequisiteStatus.SKIPPED, "alert state was not probed (no data source)"


def _check_signal_present(
    prereq: Prerequisite, ctx: IncidentContext
) -> tuple[PrerequisiteStatus, str]:
    signal = _norm(prereq.signal)
    if not signal:
        return (
            PrerequisiteStatus.UNKNOWN,
            "prerequisite declares check=signal_present but no signal",
        )
    if not ctx.observed_signals:
        return PrerequisiteStatus.SKIPPED, "no signals were observed for this incident"
    if signal in {_norm(s) for s in ctx.observed_signals}:
        return PrerequisiteStatus.SATISFIED, f"signal {prereq.signal!r} observed"
    return PrerequisiteStatus.FAILED, f"signal {prereq.signal!r} was not observed"


def evaluate_prerequisites(
    runbook: ExecutableRunbook, ctx: IncidentContext, *, now: datetime | None = None
) -> list[PrerequisiteResult]:
    """Evaluate every declared prerequisite. An unrecognised ``check`` is UNKNOWN —
    never silently satisfied, so a typo'd check on a mandatory row blocks execution
    instead of waving it through."""
    at = now or datetime.now(UTC)
    out: list[PrerequisiteResult] = []
    for prereq in runbook.prerequisites:
        check = _norm(prereq.check)
        if check == "incident_active":
            status, detail = _check_incident_active(ctx, now=at)
        elif check == "service_scope":
            status, detail = _check_service_scope(runbook)
        elif check == "alert_firing":
            status, detail = _check_alert_firing(ctx)
        elif check == "signal_present":
            status, detail = _check_signal_present(prereq, ctx)
        else:
            status, detail = (
                PrerequisiteStatus.UNKNOWN,
                f"no evaluator for check {prereq.check!r} — needs a human",
            )
        out.append(
            PrerequisiteResult(
                id=prereq.id,
                description=prereq.description,
                mandatory=prereq.mandatory,
                check=prereq.check,
                status=status,
                detail=detail,
            )
        )
    return out


# ── the verdict ──────────────────────────────────────────────────────────────


def evaluate(
    runbook: ExecutableRunbook, ctx: IncidentContext, *, now: datetime | None = None
) -> ApplicabilityResult:
    """The full applicability verdict for one runbook against one incident.

    Order matters and is deliberate: lifecycle refusal first (a DRAFT runbook is not
    worth explaining a facet mismatch about), then facet contradictions, then
    prerequisites. Everything is still *evaluated* — the result carries all facets and
    all prerequisites regardless of the status — so the UI can show the full picture
    behind a single-line refusal.
    """
    facets = evaluate_facets(runbook, ctx)
    prereqs = evaluate_prerequisites(runbook, ctx, now=now)

    reasons: list[str] = []
    blocking: list[str] = []
    warnings: list[str] = []

    for facet in facets:
        if facet.verdict is FacetVerdict.MATCH:
            reasons.append(facet.detail)
        elif (facet.verdict is FacetVerdict.UNKNOWN and facet.name in _DISQUALIFYING) or (
            facet.verdict is FacetVerdict.MISMATCH and facet.name not in _DISQUALIFYING
        ):
            warnings.append(f"{facet.name}: {facet.detail}")

    for prereq in prereqs:
        if prereq.status is PrerequisiteStatus.SATISFIED:
            continue
        line = f"prerequisite {prereq.id!r}: {prereq.detail}"
        if prereq.mandatory and prereq.status is PrerequisiteStatus.FAILED:
            blocking.append(line)
        else:
            warnings.append(line)

    lifecycle_reason = runbook.executability_reason()
    disqualified = [
        f.detail for f in facets if f.name in _DISQUALIFYING and f.verdict is FacetVerdict.MISMATCH
    ]
    mandatory_unknown = [
        f"prerequisite {p.id!r} could not be evaluated: {p.detail}"
        for p in prereqs
        if p.mandatory and p.status is PrerequisiteStatus.UNKNOWN
    ]

    if lifecycle_reason:
        status = ApplicabilityStatus.BLOCKED
        blocking.insert(0, lifecycle_reason)
    elif disqualified:
        status = ApplicabilityStatus.NOT_APPLICABLE
        blocking = disqualified + blocking
    elif blocking:
        status = ApplicabilityStatus.BLOCKED
    elif mandatory_unknown:
        status = ApplicabilityStatus.UNKNOWN
        blocking = mandatory_unknown
    else:
        status = ApplicabilityStatus.APPLICABLE
        satisfied = [p.id for p in prereqs if p.status is PrerequisiteStatus.SATISFIED]
        if satisfied:
            reasons.append(f"prerequisites satisfied: {', '.join(satisfied)}")

    return ApplicabilityResult(
        runbook_id=runbook.id,
        runbook_version=runbook.version,
        status=status,
        reasons=reasons,
        blocking_reasons=blocking,
        warnings=warnings,
        facets=facets,
        prerequisites=prereqs,
    )


__all__ = [
    "ApplicabilityResult",
    "ApplicabilityStatus",
    "FacetResult",
    "FacetVerdict",
    "IncidentContext",
    "PrerequisiteResult",
    "PrerequisiteStatus",
    "evaluate",
    "evaluate_facets",
    "evaluate_prerequisites",
    "service_matches",
]
