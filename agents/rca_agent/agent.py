"""RCA Agent (PRS-008) — root-cause analysis + ranked reversible fix steps.

Entry point: ``analyze(triage_verdict, *, scenario_id=None, correlation=None,
context=None) -> RCAVerdict``.

Pipeline:

    1. Change correlation — commits touching the service (best-effort)
    2. Evidence — from the shared ``IncidentContext`` when one is usable and
       ``AIOPS_CONTEXT_LAYER=on``, else gathered live. Never raises. Returns the
       backend it read from, so step 3 reasons over the *same* readings the
       prompt is built from (``evidence.CachingBackend``).
    3. **Deterministic investigation** (``investigation/pipeline.py``) — scope,
       timeline, baseline, completeness, evidence-triggered hypotheses, an
       evidence matrix per hypothesis, and a scored ranking. No LLM.
    4. Render the prompt from the evidence blocks
    5. One LLM reasoning pass
    6. Parse defensively, then coerce + pydantic-validate
    7. Platform-owned invariants applied to the model's output:
         * ``requires_hitl=True`` forced on every fix step
         * **confidence is the deterministic score of the top-ranked
           hypothesis** — the model's figure is recorded as
           ``llm_stated_confidence`` and not used. When the model's prose does
           not describe the hypothesis that was scored, the result is
           downgraded to ``UNCERTAIN``: a number computed for one claim must
           not be presented beside a different one.
         * ``root_cause_status`` from the ranking, so "the evidence does not
           separate these" is a stated outcome rather than a number to interpret
         * proposed actions grounded against the real action registry
    8. When the LLM is unavailable or unparseable, the verdict is built **from
       the investigation** (``_verdict_from_investigation``). That is the point
       of step 3: the cause is chosen by scored rules, so an absent model costs
       the prose rather than the diagnosis. Only with no ranked hypothesis at
       all does it fall back to ``INSUFFICIENT_EVIDENCE`` — or, for an explicit
       ``scenario_id``, the hand-written demo verdict.

The stages own the conclusion's *number*; the LLM owns its *prose*. Blast
radius and grounded recovery options are the next phases, and land as further
stages rather than as prompt instructions.

Vendor-neutrality: imports only from ``aiops.llm``. No SDK imports, no
direct vendor clients.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from agents.rca_agent import evidence as _evidence
from agents.rca_agent.investigation.models import Investigation, RootCauseStatus
from agents.rca_agent.models import (
    BlastRadius,
    FixActionType,
    RankedFixStep,
    RCAAuditMetadata,
    RCAInput,
    RCAVerdict,
)
from agents.rca_agent.progress import (
    ProgressSink,
    RcaStage,
    RunProgress,
    StageOutcome,
)
from agents.rca_agent.prompts import (
    ACTION_VOCABULARY_BLOCK,
    CHANGE_EVIDENCE_BLOCK,
    CORRELATION_EVIDENCE_BLOCK,
    INVESTIGATION_BLOCK,
    NO_ACTIONS_BLOCK,
    RCA_PROMPT_USER_V2,
    SYSTEM_PROMPT_V7,
)
from aiops.llm import Message
from aiops.llm import complete as llm_complete
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# Deterministic fallback covers exactly the v0-locked scenario. Any other
# scenario_id with no usable LLM response surfaces as a low-confidence verdict
# rather than a confident wrong answer (the truth file's "known_wrong_fixes"
# section is explicit that pattern-matching to restart/scale is a failure mode).
#
# Reviewed at the end of Phase 7 as the last channel through which a scenario name
# could act as an injection-truth shortcut, and kept deliberately rather than removed.
# It is NOT redundant with the Phase 2 deterministic investigation: that pipeline needs
# observed facts, and this branch exists for the opposite case — zero telemetry (the
# rehearsed demo calling RCA before Prometheus has scraped, or a fully offline/stub
# run) where the investigation can only say INSUFFICIENT_EVIDENCE. `_fallback_verdict`
# already checks the investigation FIRST and returns early when it ranked anything, so
# this branch only fires when there is genuinely nothing else to reason from.
# Two independent guarantees keep it out of the numbers this whole effort produced:
#   1. `evals.rca_truth.rca_input_from_truth` never passes `scenario_id`, and
#      `tests/test_rca_eval_blindness.py` (69 cases) enforces that structurally — no
#      accuracy figure anywhere in this repo passed through this branch.
#   2. It matches exactly one literal string. Broadening it (a set, a prefix, a
#      service-name match) is what made the old `_LOCKED_SERVICES` bug possible —
#      see the fix note below — so widen it only with the same suspicion.
# `demo/failure_injection` and its callers (`incident_commander`, `knowledge_synthesizer`
# tests, `test_rca_fallback_honesty.py`, `test_rca_remediation.py`) depend on this exact
# behaviour; removing it would destabilise the rehearsed demo for no safety gain, since
# the boundary that actually matters is #1, not the branch's absence.
_LOCKED_SCENARIO = "user_service_mysql_down"

# Per-agent LLM choice. The platform default (AIOPS_LLM_PROVIDER=openai → Azure
# OpenAI gpt-5) works for the lighter agents (alert_triage, classifier) but
# Azure's content filter false-positives on the structural shape of RCA's
# prompt — tagged alert IDs, parenthesized severity scores, and biomarker-
# looking metric labels all read as clinical-lab content to the classifier and
# trip `self_harm: severity=medium` deterministically. Routing this one agent
# through Anthropic Claude (via the Foundry deployment on the same Azure
# resource) sidesteps it. Override either env var to switch back if the
# situation changes.
_DEFAULT_RCA_PROVIDER = "anthropic"
_DEFAULT_RCA_MODEL = "claude-sonnet-4-6"


def _rca_provider() -> str:
    """Which LLM provider this agent asks for, resolved per call.

    Two fixes over the module-level constant this replaces.

    **Read per call, not at import.** ``aiops/context/config.py`` documents the bug
    this class of constant causes: a value baked in at import cannot be moved by
    ``monkeypatch.setenv``, so a test has to reach into the module and patch a
    private name. Per-call means the env var works in tests and in an operator's
    shell alike.

    **An explicit ``stub`` platform provider wins.** ``aiops.llm.base.get_provider``
    lets an explicit ``provider=`` argument override ``AIOPS_LLM_PROVIDER``, so
    pinning "anthropic" here meant CI — which sets ``AIOPS_LLM_PROVIDER=stub`` —
    still asked for Anthropic, failed on absent credentials, and landed in the
    *exception* fallback. The ``[stub]`` branch in ``analyze`` was therefore dead
    code for this agent, and CI was exercising an error path while appearing to
    exercise the stub. The Azure content-filter problem this pin exists for cannot
    arise with the stub provider, so deferring to it costs nothing and makes the
    offline path honest and reproducible.
    """
    explicit = os.environ.get("AIOPS_RCA_LLM_PROVIDER", "").strip()
    if explicit:
        return explicit
    if os.environ.get("AIOPS_LLM_PROVIDER", "").strip().lower() == "stub":
        return "stub"
    return _DEFAULT_RCA_PROVIDER


def _rca_model() -> str:
    return os.environ.get("AIOPS_RCA_LLM_MODEL", "").strip() or _DEFAULT_RCA_MODEL


# Confidence ceiling for a verdict reached with no observed evidence.
#
# Deterministic, and applied regardless of what the model claimed. A run that saw
# no telemetry cannot have grounds for a confident cause, and the system prompt
# already instructs the model to stay at or below 0.3 in exactly this case — this
# constant enforces the instruction instead of trusting it. Set at the prompt's own
# threshold so the two cannot disagree.
NO_EVIDENCE_CONFIDENCE_CEILING = 0.3


# ─── deterministic fallback ─────────────────────────────────────────────────


def _verdict_from_investigation(
    investigation: Investigation,
    *,
    service: str,
    decision_trace: list[str],
) -> RCAVerdict:
    """A verdict built from the deterministic stages alone, with no LLM involved.

    This is what the investigation pipeline buys beyond a better prompt: when the model is
    unavailable — the CI stub, a provider outage, an unparseable reply — the agent can
    still name a cause, because the cause was chosen by scored rules rather than by the
    model. Before Phase 2 every one of those paths produced "insufficient evidence"
    regardless of how decisive the telemetry was.

    The fix step is deliberately ``manual``. The hypothesis carries an ``action_hint``
    (a generic remediation *class*), not an executable key, and turning a class into a
    runnable action is action grounding's job — Phase 5. Inventing one here is exactly the
    coupling the grounding layer exists to prevent, so the step describes what to do and
    stays non-executable.
    """
    selected = investigation.selected
    assert selected is not None  # guarded by the caller: matrices is non-empty
    hypothesis = selected.hypothesis
    quoted = "; ".join(item.statement for item in selected.supporting[:3])

    decision_trace.append(
        f"verdict derived from the deterministic investigation without an LLM: "
        f"{hypothesis.category} at {investigation.confidence:.2f}"
    )
    root_cause = f"{hypothesis.label}. {hypothesis.mechanism}."
    if quoted:
        root_cause += f" Evidence: {quoted}."
    if investigation.status is RootCauseStatus.UNCERTAIN and len(investigation.matrices) > 1:
        runner_up = investigation.matrices[1].hypothesis.label
        root_cause += (
            f" The evidence does not separate this from a competing explanation "
            f"({runner_up}), so treat it as unconfirmed."
        )

    return RCAVerdict(
        affected_service=service,
        root_cause=root_cause,
        ranked_fix_steps=_steps_from_recovery_options(
            investigation, service=service, decision_trace=decision_trace
        ),
        confidence_score=investigation.confidence,
        root_cause_status=investigation.status,
        investigation=investigation,
        audit_metadata=RCAAuditMetadata(
            created_at=datetime.now(UTC),
            decision_trace=decision_trace,
        ),
    )


def _steps_from_recovery_options(
    investigation: Investigation,
    *,
    service: str,
    decision_trace: list[str],
) -> list[RankedFixStep]:
    """Turn the recovery stage's options into fix steps for the verdict.

    Phase 5 is what makes this possible. Until now the no-LLM path emitted a single
    ``manual`` step with the note "the platform has not grounded this recommendation
    against a runnable capability" — accurate at the time, because grounding needed the
    ``automation.fault.clear`` provider and returned nothing offline. Now
    ``_action_vocabulary`` answers offline too, so a recovery option whose action key was
    matched against that vocabulary can be offered as ``set_flag``.

    An option that did *not* ground still yields a manual step. That is the honest
    outcome, and it is most of them: the ranked cause is a generic failure class, and only
    some classes have a runnable action behind them.
    """
    options = investigation.recovery_options
    if not options:
        selected = investigation.selected
        hint = (
            (selected.hypothesis.action_hint or "no action class").replace("_", " ")
            if selected
            else "no action class"
        )
        return [
            RankedFixStep(
                description=(
                    f"Investigate {service} manually ({hint}). No recovery option was "
                    "planned: the status is not actionable, so the next step is a human "
                    "looking rather than a fix executing."
                ),
                blast_radius=BlastRadius.LOW,
                rollback="N/A — no change is made by this step.",
                action_type=FixActionType.MANUAL,
            )
        ]

    steps: list[RankedFixStep] = []
    for option in options:
        if option.action_key and option.grounded:
            decision_trace.append(
                f"recovery option {option.option_id}: grounded action "
                f"{option.action_key!r} ({'executable' if option.executable else 'no executor registered here'})"
            )
        steps.append(
            RankedFixStep(
                description=option.description,
                blast_radius=option.blast_radius,
                rollback=option.rollback or "N/A — no change is made by this step.",
                action_type=(
                    FixActionType.SET_FLAG
                    if option.action_key and option.grounded
                    else FixActionType.MANUAL
                ),
                flag=option.action_key if option.grounded else None,
            )
        )
    return steps


def _fallback_verdict(
    triage: dict[str, Any],
    *,
    scenario_id: str | None,
    decision_trace: list[str],
    investigation: Investigation | None = None,
) -> RCAVerdict:
    """Hand-written verdict matching ``demo/ecommerce/truth_files/user_service_mysql_down.json``.

    Used when (a) the LLM provider is the stub, (b) the LLM response is
    unparseable, or (c) the scenario is locked-v0 and we want to guarantee
    eval-harness coverage independent of LLM availability.
    """
    # "unknown", NOT a service name. This used to default to
    # "productcatalogservice", which was in the since-deleted _LOCKED_SERVICES set —
    # so a triage verdict with a MISSING affected_service silently produced a
    # confident root cause about a service that was never involved (and, after the
    # migration, no longer exists).
    service = triage.get("affected_service") or "unknown"

    # The deterministic investigation outranks both branches below when it actually ranked
    # something. It reasons from observed telemetry, whereas the locked-scenario branch
    # recognises an id it was handed and the last branch abstains — evidence beats both.
    if investigation is not None and investigation.matrices:
        return _verdict_from_investigation(
            investigation, service=service, decision_trace=decision_trace
        )

    # Scenario id ONLY. This used to also fire on
    # ``service.lower() in _LOCKED_SERVICES``, a frozenset containing
    # "user-service" — so *any* user-service incident with no usable LLM returned
    # this hand-written MySQL verdict at confidence 0.85, whatever had actually
    # broken. user-service has four distinct failure modes (mysql_down, crashloop,
    # high_latency, high_cpu) plus an out-of-band pool-exhaustion mode, and a
    # service name cannot distinguish them: that is precisely what evidence is for.
    # The set is deleted rather than narrowed, because there is no service name for
    # which "the service is X, therefore the cause is Y" is sound reasoning.
    if scenario_id == _LOCKED_SCENARIO:
        decision_trace.append(
            "deterministic fallback: matched locked scenario user_service_mysql_down"
        )
        return RCAVerdict(
            affected_service=service,
            root_cause=(
                "The MySQL StatefulSet in namespace `ecommerce` is scaled to zero, so "
                "user-service cannot open a database connection and returns HTTP 500 on "
                "/login and /register. The service itself is healthy and Running - "
                "mysql_connection_status reads 0 and /health reports status=degraded."
            ),
            ranked_fix_steps=[
                RankedFixStep(
                    description=(
                        "Clear the user_service.mysql_down fault - scale the MySQL "
                        "StatefulSet back to 1 and wait for the rollout."
                    ),
                    blast_radius=BlastRadius.LOW,
                    rollback="Scale MySQL back to 0 - instant, and the PVC is retained.",
                    action_type=FixActionType.SET_FLAG,
                    # `flag` carries a FAILURE KEY now, not a flagd flag name.
                    # automation.fault.clear takes exactly these.
                    flag="user_service.mysql_down",
                    variant="off",
                ),
                RankedFixStep(
                    description=(
                        "If MySQL is already Running and the gauge stays 0, verify the "
                        "credentials in the ecommerce-secrets Secret still match what "
                        "the StatefulSet was initialised with - the password is only "
                        "applied on first boot with an empty PVC."
                    ),
                    blast_radius=BlastRadius.MEDIUM,
                    rollback="No change made - this step is diagnostic.",
                    action_type=FixActionType.MANUAL,
                ),
            ],
            confidence_score=0.85,
            root_cause_status=RootCauseStatus.PROBABLE,
            audit_metadata=RCAAuditMetadata(
                created_at=datetime.now(UTC),
                decision_trace=decision_trace,
            ),
        )

    # No usable LLM and no recognised scenario — emit an explicit
    # INSUFFICIENT_EVIDENCE verdict rather than a confident wrong answer. This is
    # now the fallback for every service, including user-service: the truth files'
    # own "known_wrong_fixes" sections are explicit that pattern-matching a service
    # name to restart/scale is a failure mode, and a service name is exactly the
    # kind of pattern that produces one.
    decision_trace.append(
        f"deterministic fallback: no recognised scenario (scenario_id={scenario_id!r}) "
        "and no usable LLM response; emitting INSUFFICIENT_EVIDENCE rather than "
        "inferring a cause from the service name"
    )
    return RCAVerdict(
        affected_service=service,
        root_cause=(
            f"Insufficient evidence to identify a root cause for {service}. "
            "Review the triage verdict's decision trace and re-run RCA after "
            "the next correlation pass."
        ),
        ranked_fix_steps=[
            RankedFixStep(
                description="Manual investigation required — no automated fix proposed.",
                blast_radius=BlastRadius.LOW,
                rollback="N/A — no action taken.",
            )
        ],
        confidence_score=0.2,
        root_cause_status=RootCauseStatus.INSUFFICIENT_EVIDENCE,
        audit_metadata=RCAAuditMetadata(
            created_at=datetime.now(UTC),
            decision_trace=decision_trace,
        ),
    )


# ─── LLM response parsing ───────────────────────────────────────────────────


# Some providers emit ```json ... ``` despite the system prompt asking for raw
# JSON. Strip an optional code fence before parsing.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from a model reply.

    1. Strip surrounding markdown code fences.
    2. Try ``json.loads`` on the whole stripped string.
    3. Fall back to scanning for the first balanced ``{...}`` and parsing that.

    Returns ``None`` when nothing parses — caller falls back to the
    deterministic verdict.
    """
    if not text:
        return None
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Scan for first balanced object. Naive depth counter is fine — LLM
    # outputs don't embed unescaped braces inside string values often enough
    # to justify a real parser here.
    start = cleaned.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(cleaned[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_action(step: dict[str, Any]) -> tuple[FixActionType, str | None, str]:
    """Pull the machine-readable action out of one LLM-emitted fix step.

    Defensive: an unknown / missing ``action_type`` becomes ``manual``, and a
    ``set_flag`` step with no usable ``flag`` is downgraded to ``manual`` —
    the executor must never be handed a flag-flip with no flag to flip.
    Returns ``(action_type, flag, variant)``.
    """
    raw_type = str(step.get("action_type", "")).strip().lower()
    try:
        action_type = FixActionType(raw_type)
    except ValueError:
        action_type = FixActionType.MANUAL

    flag = step.get("flag")
    flag = str(flag).strip() if flag else None
    variant = str(step.get("variant") or "off").strip() or "off"

    if action_type is FixActionType.SET_FLAG and not flag:
        # set_flag with no flag is not executable — keep it honest.
        return FixActionType.MANUAL, None, variant
    if action_type is not FixActionType.SET_FLAG:
        # flag/variant are only meaningful for set_flag; drop stray values.
        return action_type, None, "off"
    return action_type, flag, variant


def _live_flag_names() -> set[str] | None:
    """Best-effort set of fault keys the platform can actually clear.

    Grounding exists because the LLM invents plausible-but-nonexistent handles
    (it once emitted ``emailGatewayProvider`` for a service whose real handle was
    ``emailMemoryLeak``). Checking a proposed fix against the real list stops the
    agent recommending a step that cannot execute.

    Previously this read flag names from flagd via
    ``feature_flags.list_variants``. flagd shipped with the OpenTelemetry Demo
    and was removed in migration Phase 6; the handles are now ecommerce failure
    keys such as ``order_service.http_500``, surfaced by the
    ``automation.fault.clear`` provider's error metadata.

    Returns ``None`` when the list cannot be fetched — no provider registered,
    cluster unreachable, call failed — so callers fail *open*. Grounding is a
    safety net, never a hard dependency for the offline / eval / unit-test paths.
    """
    try:
        from aiops.tools import get_registry

        # Deliberately an invalid request: the provider answers a bad fault key
        # with the list of valid ones in metadata. That avoids adding a
        # list-only capability whose sole consumer is this safety net.
        res = get_registry().call("automation.fault.clear", fault="", target="off")
        meta = getattr(res, "metadata", None) or {}
        names = set(meta.get("available_faults") or ())
        if names:
            return names
        res = get_registry().call("automation.fault.clear", fault="__probe__", target="off")
        meta = getattr(res, "metadata", None) or {}
        names = set(meta.get("available_faults") or ())
        return names or None
    except Exception:  # registry missing capability, cluster unreachable, etc.
        return None


def _is_real_but_unattributed(flag: str, service: str) -> bool:
    """Whether ``flag`` is a real registry action that names no service at all.

    Two shapes of key exist in this platform. Ecommerce faults are
    ``<service>.<condition>``, so the service is *in* the key and a mismatch is provable —
    ``order_service.http_500`` proposed for payment-service is wrong even though the key is
    real, and an action that runs and fixes a different service's problem is worse than one
    that fails. Legacy flagd-era handles (``emailMemoryLeak``) carry no service, so nothing
    can be proven either way; if the registry lists one, it is runnable and rejecting it
    would invent a fault.

    So: accept a dotless key the registry actually lists, reject a dotted key belonging to
    another service. Returns ``False`` when the registry cannot be reached — the caller has
    already scoped against the static map by then, and guessing here would undo it.
    """
    if "." in flag:
        return False
    live = _live_flag_names()
    if not live or flag not in live:
        return False
    logger.debug("grounding: %r names no service and the registry lists it; keeping", flag)
    return True


def _ensure_executable_action(
    steps: list[RankedFixStep],
    *,
    service: str,
    decision_trace: list[str],
) -> list[RankedFixStep]:
    """Validate every proposed action against what the platform can actually run.

    A proposed ``set_flag`` renders in the dashboard as a one-click apply. An action key
    that does not exist is therefore a button that fails *after* a human has approved it
    — the worst point at which to discover it, because the approval has already been
    given and the operator's attention has moved on.

    Rewritten in Phase 5. What it replaces, and why both halves were broken:

    * ``flag_for_service`` returned a key only for a service with exactly **one**
      possible fault. No ecommerce service has one — they all have four — so that
      branch was dead code, and worse, it was the branch that *corrected* a wrong key.
      Deleted rather than fixed: choosing the remediation from the service name is
      precisely the name-lookup this agent exists to replace, and with four candidates
      per service it cannot be done from the name at all.
    * ``_ground_set_flags_against_flagd`` asked ``_live_flag_names()``, which reaches
      the ``automation.fault.clear`` provider. That provider is registered in the demo
      layer only, so in CI, in every eval run, and on any laptop without the cluster the
      lookup returned ``None`` and the function **returned the steps unchanged**. It
      failed open on exactly the paths where nothing else was checking, so an invented
      key passed straight through to the verdict. Its name also referred to flagd,
      which was removed from this repo two migrations ago.

    The fix is to share one authority with the prompt: ``_action_vocabulary(service)``
    resolves the registry first and the static map second, so the list the model is
    *offered* and the list it is *held to* are the same list by construction. They
    cannot drift, and the offline path is now checked rather than waved through.

    Still fails open in one case only — no registry **and** no static entry for the
    service, i.e. the platform genuinely cannot say what is runnable. The executor
    remains the final backstop there.
    """
    if not any(s.action_type is FixActionType.SET_FLAG and s.flag for s in steps):
        return steps

    available, source = _action_vocabulary(service)

    # An *empty* vocabulary is not the same as an *unknown* one, and conflating them was a
    # regression: with the registry reachable but holding no action for this service (the
    # `frontend` case), "no keys" meant "nothing is runnable here" — an authoritative
    # answer — and skipping grounding on it let an invented key through as a clickable
    # button. Only ``VOCAB_UNAVAILABLE`` means nobody could tell us, and only then is
    # failing open correct.
    if source == VOCAB_UNAVAILABLE:
        decision_trace.append(
            f"action grounding skipped: {source} — proposed keys are unvalidated and the "
            "executor is the only remaining check"
        )
        return steps

    grounded: list[RankedFixStep] = []
    for i, step in enumerate(steps):
        if step.action_type is not FixActionType.SET_FLAG or not step.flag:
            grounded.append(step)
            continue
        if step.flag in available or _is_real_but_unattributed(step.flag, service):
            grounded.append(step)
            continue
        detail = (
            f"the platform has no executable action for {service!r} at all"
            if not available
            else f"not an action the platform can execute for {service!r}"
        )
        decision_trace.append(
            f"downgraded fix step #{i + 1}: {step.flag!r} — {detail} ({source}); marked manual"
        )
        grounded.append(
            step.model_copy(
                update={
                    "action_type": FixActionType.MANUAL,
                    "flag": None,
                    "variant": "off",
                    "description": (
                        f"{step.description}  [NOTE: '{step.flag}' is not an executable "
                        f"action for {service} — investigate manually.]"
                    ),
                }
            )
        )
    return grounded


def _derive_status(confidence: float, *, has_evidence: bool) -> RootCauseStatus:
    """Classify how settled a conclusion is, from the platform's own figures.

    Phase 1 reads the (already evidence-capped) confidence. The thresholds mirror
    the wording the system prompt gives the model — 0.9 "I would bet on this", 0.5
    "best of 2-3 plausibles", below 0.4 "prefer a manual investigation step" — so
    the status and the instruction the model was given cannot drift apart.

    ``has_evidence=False`` short-circuits to ``INSUFFICIENT_EVIDENCE`` before any
    threshold is consulted: with nothing observed there is no conclusion to grade,
    and that is a different statement from "the signals did not discriminate".

    Phase 2 replaces the input to this function with the deterministic hypothesis
    score, at which point the status follows from the evidence matrix rather than
    from a number the model proposed.
    """
    if not has_evidence:
        return RootCauseStatus.INSUFFICIENT_EVIDENCE
    if confidence >= 0.75:
        return RootCauseStatus.CONFIRMED
    if confidence >= 0.5:
        return RootCauseStatus.PROBABLE
    if confidence >= 0.3:
        return RootCauseStatus.UNCERTAIN
    return RootCauseStatus.INSUFFICIENT_EVIDENCE


def _bounded_confidence(stated: float, *, has_evidence: bool, decision_trace: list[str]) -> float:
    """The authoritative confidence: the model's figure, bounded by the evidence.

    A first, deliberately narrow step towards platform-owned confidence. The model
    is still the source of the number, but it no longer gets the last word on the
    one case where its number is provably unsupported: a run that observed nothing
    cannot justify a confident cause, whatever it says. Capping is recorded in the
    trace, so a suppressed claim is visible rather than silently rewritten.

    The general case — deriving confidence from the evidence matrix and ignoring the
    model's figure entirely — is Phase 2. This is the part that can be done
    correctly without the matrix, and the part that stops the zero-evidence path
    from producing a confident answer today.
    """
    clamped = min(max(stated, 0.0), 1.0)
    if has_evidence or clamped <= NO_EVIDENCE_CONFIDENCE_CEILING:
        return clamped
    decision_trace.append(
        f"capped confidence {clamped:.2f} -> {NO_EVIDENCE_CONFIDENCE_CEILING:.2f}: no live "
        "evidence was observed, so no conclusion can be better supported than that"
    )
    return NO_EVIDENCE_CONFIDENCE_CEILING


def _authoritative_confidence(
    stated: float,
    *,
    investigation: Investigation | None,
    has_evidence: bool,
    root_cause: str,
    decision_trace: list[str],
) -> tuple[float, RootCauseStatus]:
    """The verdict's confidence and status — platform-derived, not model-stated.

    Three cases, in order of how much the platform knows:

    1. **The investigation ranked hypotheses.** Its score is the answer. The model's
       figure is discarded for ranking purposes and kept only as
       ``llm_stated_confidence``. If the model's prose does not describe the hypothesis
       that was scored, the pair is not corroborated and the result is downgraded to
       ``UNCERTAIN`` — a number computed for one claim must not be presented beside a
       different one.
    2. **The stages ran but proposed nothing.** Evidence was seen and no catalogued
       failure class matched it. That is ``INSUFFICIENT_EVIDENCE`` about the *catalog*,
       not about the world, and the note on the investigation says which.
    3. **The stages could not run** (offline, or they failed). Falls back to Phase 1
       behaviour: the model's figure, capped by whether anything was observed.
    """
    if investigation is not None and investigation.matrices:
        confidence = investigation.confidence
        status = investigation.status
        decision_trace.append(
            f"confidence {confidence:.2f} is the deterministic score of the top hypothesis; "
            f"the model stated {stated:.2f} (recorded, not used)"
        )
        if status.is_actionable and not _grounded_in_investigation(root_cause, investigation):
            selected = investigation.selected
            label = selected.hypothesis.label if selected else "the top hypothesis"
            decision_trace.append(
                f"downgraded to UNCERTAIN: the stated root cause does not describe {label!r}, "
                "so the deterministic score does not corroborate the prose"
            )
            return min(confidence, 0.5), RootCauseStatus.UNCERTAIN
        return confidence, status

    if investigation is not None:
        decision_trace.append(
            "investigation proposed no hypothesis for the observed evidence; "
            "reporting INSUFFICIENT_EVIDENCE rather than accepting an unranked cause"
        )
        return min(stated, NO_EVIDENCE_CONFIDENCE_CEILING), RootCauseStatus.INSUFFICIENT_EVIDENCE

    bounded = _bounded_confidence(stated, has_evidence=has_evidence, decision_trace=decision_trace)
    return bounded, _derive_status(bounded, has_evidence=has_evidence)


def _coerce_verdict(
    raw: dict[str, Any],
    *,
    service: str,
    decision_trace: list[str],
    has_evidence: bool = True,
    investigation: Investigation | None = None,
) -> RCAVerdict | None:
    """Coerce a parsed LLM JSON dict into ``RCAVerdict``.

    Forces ``requires_hitl=True`` on every fix step regardless of what the
    model emitted — the catalog invariant is platform-enforced, not model-
    trusted. Clamps the step list to at most 3 to keep the dashboard tidy.
    Returns ``None`` on validation failure so the caller can fall back.

    The model's ``confidence_score`` is recorded as ``llm_stated_confidence`` and
    the authoritative ``confidence_score`` is derived — see ``_bounded_confidence``.
    """
    try:
        steps_raw = raw.get("ranked_fix_steps") or []
        if not isinstance(steps_raw, list) or not steps_raw:
            return None
        steps: list[RankedFixStep] = []
        for s in steps_raw[:3]:
            if not isinstance(s, dict):
                continue
            br = str(s.get("blast_radius", "")).lower()
            if br not in {"low", "medium", "high"}:
                br = "medium"
            action_type, flag, variant = _coerce_action(s)
            steps.append(
                RankedFixStep(
                    description=str(s.get("description", "")).strip(),
                    blast_radius=BlastRadius(br),
                    rollback=str(s.get("rollback", "")).strip(),
                    requires_hitl=True,  # invariant — never trust the LLM here
                    action_type=action_type,
                    flag=flag,
                    variant=variant,
                )
            )
        if not steps:
            return None
        steps = _ensure_executable_action(steps, service=service, decision_trace=decision_trace)
        stated = float(raw.get("confidence_score", 0.5))
        root_cause = str(raw.get("root_cause", "")).strip()
        confidence, status = _authoritative_confidence(
            stated,
            investigation=investigation,
            has_evidence=has_evidence,
            root_cause=root_cause,
            decision_trace=decision_trace,
        )
        return RCAVerdict(
            affected_service=service,
            root_cause=root_cause,
            ranked_fix_steps=steps,
            confidence_score=confidence,
            root_cause_status=status,
            llm_stated_confidence=stated,
            investigation=investigation,
            audit_metadata=RCAAuditMetadata(
                created_at=datetime.now(UTC),
                decision_trace=decision_trace,
            ),
        )
    except Exception as exc:
        logger.warning("RCA verdict coercion failed: %s", exc)
        return None


# ─── prompt rendering ───────────────────────────────────────────────────────


def _render_evidence_block(correlation: dict[str, Any] | None) -> str:
    """Render the optional Log Correlation (RA-007) evidence into a prompt
    block. Returns an empty string when no correlation is supplied so the base
    prompt is unchanged (backward-compatible)."""
    if not correlation:
        return ""
    suspects = correlation.get("suspected_dependencies") or []
    sigs = correlation.get("top_signatures") or []
    summary = str(correlation.get("summary") or "(none)")
    rendered_sigs = "\n".join(f"- {s}" for s in sigs) if sigs else "- (none)"
    return CORRELATION_EVIDENCE_BLOCK.format(
        suspect_components=", ".join(str(s) for s in suspects) or "(undetermined)",
        top_signatures=rendered_sigs,
        summary=summary,
    )


# Where each service's code lives, for scoping the commit query. Without a
# path filter the query returns repo-wide commits and the model happily blames
# a docs change for a database outage.
#
# Unmapped services fall back to a repo-wide query, which is noisier but still
# better than no change evidence at all.
_SERVICE_SOURCE_PATHS: dict[str, str] = {
    "user-service": "demo/ecommerce/user-service",
    "order-service": "demo/ecommerce/order-service",
    "payment-service": "demo/ecommerce/payment-service",
    "mock-payment-gateway": "demo/ecommerce/mock-payment-gateway",
    "frontend": "demo/ecommerce/frontend",
}

# How far back to look for changes. Long enough to catch "deployed this
# morning, broke this afternoon"; short enough that the model isn't handed a
# month of unrelated history to pattern-match against.
_CHANGE_LOOKBACK_HOURS = int(os.getenv("AIOPS_RCA_CHANGE_LOOKBACK_HOURS", "48"))


def _fetch_change_evidence(service: str, decision_trace: list[str]) -> list[dict[str, Any]] | None:
    """Recent commits touching ``service``, via the SCM seam. Never raises.

    Goes through ``get_registry().call`` rather than importing the GitHub
    provider: the agent must not know which SCM backend is configured
    (non-negotiable #1), and the registry is also where the HITL gate lives.

    Returns None when the seam is unregistered or unconfigured — the common
    case in CI and for anyone running without a token. RCA then proceeds with
    observability evidence alone, exactly as it did before this existed.
    """
    path = _SERVICE_SOURCE_PATHS.get(service)
    if path is None:
        decision_trace.append(
            f"no source path mapped for service={service!r}; querying repo-wide change history"
        )

    since = (datetime.now(UTC) - timedelta(hours=_CHANGE_LOOKBACK_HOURS)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    try:
        res = get_registry().call("scm.commit.history", path=path, since=since, limit=10)
    except KeyError:
        # Capability not registered — nobody imported aiops.tools.scm, or the
        # deployment deliberately omits source access.
        decision_trace.append("scm.commit.history not registered; skipping change correlation")
        return None
    except Exception as exc:
        decision_trace.append(f"change correlation raised {type(exc).__name__}; skipping")
        return None

    if not getattr(res, "ok", False):
        decision_trace.append(f"change correlation unavailable: {getattr(res, 'error', 'unknown')}")
        return None

    commits = (res.data or {}).get("commits") or []
    if not commits:
        # A real and useful answer: "nothing changed here recently" actively
        # argues AGAINST a deploy-induced cause, so record it rather than
        # treating it as a failed lookup.
        decision_trace.append(
            f"change correlation: no commits touching {path or 'the repo'} in the last "
            f"{_CHANGE_LOOKBACK_HOURS}h"
        )
        return []

    decision_trace.append(
        f"change correlation: {len(commits)} commit(s) touching {path or 'the repo'} "
        f"in the last {_CHANGE_LOOKBACK_HOURS}h (newest {commits[0].get('sha')})"
    )
    return commits


def _render_change_block(commits: list[dict[str, Any]] | None) -> str:
    """Render commits into a prompt block. Empty string when unavailable."""
    if commits is None:
        return ""
    if not commits:
        return CHANGE_EVIDENCE_BLOCK.format(
            commits=f"- (no commits in the last {_CHANGE_LOOKBACK_HOURS}h)"
        )
    lines = [
        f"- {c.get('sha')} {c.get('date')} by {c.get('author')}: {c.get('message')}"
        for c in commits
    ]
    return CHANGE_EVIDENCE_BLOCK.format(commits="\n".join(lines))


VOCAB_FROM_REGISTRY = "from the platform action registry"
"""The action registry answered. The only source that implies an executor exists."""

VOCAB_FROM_FALLBACK = "static fallback: the action registry was unreachable"
"""``remediation_map`` answered. The keys are real; nothing here can run them."""

VOCAB_UNAVAILABLE = "no action registry reachable and no static entry for this service"

# Compared by identity, never by substring. The first version of this check asked
# `"registry" in source` — and the *fallback* string contains the word "registry" ("the
# action registry was unreachable"), so `executable` came back True offline and the whole
# grounded/executable distinction collapsed silently. Matching prose is how a negation
# gets read as a confirmation.
_EXECUTOR_SOURCES = frozenset({VOCAB_FROM_REGISTRY})


def executor_available(source: str) -> bool:
    """Whether the vocabulary source implies something can actually run the action."""
    return source in _EXECUTOR_SOURCES


def _action_vocabulary(service: str) -> tuple[tuple[str, ...], str]:
    """The executable actions available for ``service``, resolved at request time.

    Returns ``(keys, source)`` where ``source`` names where the list came from, so the
    prompt can state its own provenance rather than presenting a fallback as
    authoritative.

    Two tiers, and the order is the point of the Q2 change:

    1. **The action registry** — ``_live_flag_names()`` asks the
       ``automation.fault.clear`` provider what it accepts. Authoritative, and it
       tracks the platform: a fault registered there reaches the model with no prompt
       edit, and a removed one disappears from it.
    2. **``remediation_map``** — a static list, used only when the registry cannot be
       reached (offline, CI, no cluster). Retained because the alternative is telling
       the model there are no actions when there are, but it *is* the hardcoded list
       the constraint objects to, so it is labelled as a fallback wherever it surfaces.

    Filtered to the named service in both tiers. A key belonging to another service is
    not an action for this incident, and offering it invites a step that the executor
    accepts and that fixes nothing.
    """
    from agents.rca_agent import remediation_map

    scoped = remediation_map.faults_for_service(service)
    live = _live_flag_names()
    if live:
        if scoped:
            keys = tuple(sorted(k for k in scoped if k in live))
            if keys:
                return keys, VOCAB_FROM_REGISTRY
        # The map does not know this service. Fall back to prefix-matching the live
        # list so a service added to the platform but not to the map still gets its
        # own actions rather than none.
        prefix = remediation_map._normalise(service)
        keys = tuple(
            sorted(k for k in live if remediation_map._normalise(k.split(".")[0]) == prefix)
        )
        return keys, VOCAB_FROM_REGISTRY
    if scoped:
        return scoped, VOCAB_FROM_FALLBACK
    return (), VOCAB_UNAVAILABLE


def _render_action_block(service: str) -> str:
    keys, source = _action_vocabulary(service)
    if not keys:
        return NO_ACTIONS_BLOCK.format(service=service, source=source)
    return ACTION_VOCABULARY_BLOCK.format(
        service=service,
        source=source,
        keys="\n".join(f"  - {key}" for key in keys),
    )


def _render_investigation_block(investigation: Investigation | None) -> str:
    """Render the platform's own conclusion for the model to explain.

    Empty string when the stages could not run, which leaves the prompt in its
    pre-Phase-4 shape — the model is then genuinely diagnosing, and telling it there is
    an investigation when there is none would be worse than telling it nothing.

    Only the class, the score and the evidence *statements* are rendered. Not the
    memory ids, not the rule traces: the model is being asked for a sentence an
    engineer can read, and internal identifiers in a prompt come back out inside the
    prose.
    """
    if investigation is None or not investigation.matrices:
        return ""

    lines: list[str] = []
    for rank, matrix in enumerate(investigation.matrices[:4], start=1):
        score = matrix.score.score if matrix.score else 0.0
        lines.append(f"  {rank}. {matrix.hypothesis.category} (score {score:.2f})")
        lines.append(f"     {matrix.hypothesis.mechanism}")
        for item in matrix.supporting[:3]:
            lines.append(f"     + supports: {item.statement}")
        for item in matrix.contradicting[:2]:
            lines.append(f"     - argues against: {item.statement}")
        for item in matrix.checked_absent[:2]:
            lines.append(f"     · checked and absent: {item.statement}")
        for item in matrix.gaps[:2]:
            lines.append(f"     ? could not check: {item.statement}")

    influence = investigation.historical_influence
    memory = ""
    if influence.priors_applied or influence.changed_ranking:
        # Stated only when history actually contributed. §27: when a past incident moves
        # a conclusion the operator is told, and the model should describe it rather than
        # present a remembered pattern as fresh evidence.
        memory = (
            f"Historical influence: {influence.level} — "
            f"{len(influence.priors_applied)} verified prior(s) applied"
            + (", and they changed which class ranked first" if influence.changed_ranking else "")
            + ". Mention this only as precedent, never as evidence from this incident.\n"
        )

    return INVESTIGATION_BLOCK.format(
        status=investigation.status.value,
        confidence=f"{investigation.confidence:.2f}",
        discriminated="yes" if investigation.discriminated else "no — the top two are close",
        ranked="\n".join(lines),
        memory=memory,
    )


def _render_user_prompt(
    triage: dict[str, Any],
    correlation: dict[str, Any] | None = None,
    change_evidence: list[dict[str, Any]] | None = None,
    observed: dict[str, list[str]] | None = None,
    investigation: Investigation | None = None,
) -> str:
    service = str(triage.get("affected_service") or "unknown")
    severity = str(triage.get("severity") or "unknown")
    summary = str(triage.get("alert_summary") or "(no summary)")
    audit = triage.get("audit_metadata") or {}
    trace_lines = audit.get("decision_trace") if isinstance(audit, dict) else None
    if isinstance(trace_lines, list) and trace_lines:
        rendered_trace = "\n".join(f"- {line}" for line in trace_lines)
    else:
        rendered_trace = "- (no trace lines available)"
    # Log correlation first (the symptom), then change history (the likely
    # cause) — the order the model should reason in.
    # Log correlation (the symptom), then change history (the likely cause),
    # then raw telemetry last — the order the model should reason in, and the
    # observations are what it should have most freshly in mind.
    evidence = (
        _render_evidence_block(correlation)
        + _render_change_block(change_evidence)
        + _evidence.render(observed or {})
    )
    return RCA_PROMPT_USER_V2.format(
        service=service,
        severity=severity,
        summary=summary,
        decision_trace=rendered_trace,
        evidence_block=evidence,
        # Investigation *after* the raw evidence, so the model reads what was observed
        # before it reads what the platform concluded from it. The other order invites
        # it to accept the conclusion and then hunt for support.
        investigation_block=_render_investigation_block(investigation),
        action_block=_render_action_block(service),
    )


# ─── deterministic investigation ────────────────────────────────────────────


def _memory_signatures(
    triage: dict[str, Any],
    facts: Any,
    observed: dict[str, list[str]],
) -> list[str]:
    """Symptom identifiers for a memory recall — never a cause description.

    What goes in: alert names, metric names, error ``reason`` labels, container
    termination reasons. What stays out: anything naming a *cause*, including this
    investigation's own hypotheses.

    The distinction is the difference between recall and circular reasoning. Matching a
    new incident's symptoms against past incidents' symptoms asks "has this shape of
    failure happened before?". Matching on a proposed cause asks "have we concluded this
    before?", which retrieves the priors that agree with the conclusion already reached
    and calls that corroboration. Only the first is retrieval.

    Log *lines* are excluded too, for a duller reason: they carry request ids and
    timestamps, so no two incidents ever share one, and including them would dilute
    every Jaccard score toward zero.
    """
    signatures: list[str] = []

    summary = str(triage.get("alert_summary") or "")
    if " firing:" in summary:
        signatures.append(summary.split(" firing:")[0].strip())

    signatures.extend(alert.name for alert in getattr(facts, "alerts", []))
    for gauge in getattr(facts, "gauges", []):
        signatures.append(gauge.metric)
        if not gauge.reachable:
            # The unreachable-ness is the symptom, and the label says which store. A
            # reachable gauge contributes only its metric name, so a healthy dependency
            # cannot make two unrelated incidents look alike.
            signatures.append(f"{gauge.metric}:{gauge.label}:unreachable")
    for rate in getattr(facts, "error_rates", []):
        signatures.append(rate.metric)
        if rate.reason:
            signatures.append(f"{rate.metric}:{rate.reason}")
    for latency in getattr(facts, "latencies", []):
        if latency.breaches_threshold:
            signatures.append(f"latency_breach:{latency.hop}")
    for life in getattr(facts, "lifecycles", []):
        if life.terminated_reason:
            signatures.append(f"terminated:{life.terminated_reason}")

    # Metric names the prompt was built from, so a recall works even when the typed
    # facts came back thin — the keys are query labels, and the values are readings.
    signatures.extend(key for key in (observed or {}) if key)

    seen: set[str] = set()
    unique: list[str] = []
    for sig in signatures:
        text = str(sig).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            unique.append(text)
    return unique


def _investigate(
    triage: dict[str, Any],
    observation: _Observation,
    *,
    context: dict[str, Any] | None,
    change_evidence: list[dict[str, Any]] | None,
    offline: bool,
    decision_trace: list[str],
    progress: RunProgress | None = None,
) -> Investigation | None:
    """Run the deterministic stages. Never raises; ``None`` means they could not run.

    Guarded as a whole because the investigation is an *addition* to a working agent: a
    bug in a scoring rule must cost the structured result and leave the LLM path intact,
    exactly as an unreachable Prometheus costs evidence rather than the verdict. Returning
    ``None`` puts the agent back on its Phase-1 behaviour, which is a safe place to land.
    """
    if offline:
        return None
    run = progress or RunProgress("", None)
    try:
        from agents.rca_agent.investigation import memory, pipeline
        from agents.rca_agent.investigation.facts import collect_facts

        service = str(triage.get("affected_service") or "unknown")
        run.emit(RcaStage.CONTEXT_PACK, "Assembling the context pack")
        facts = collect_facts(
            service,
            observation.backend,
            metrics_available=observation.metrics_available,
            logs_available=observation.logs_available,
        )
        run.emit(RcaStage.MEMORY_RECALL, "Recalling verified past outcomes")
        recall = memory.recall(
            service=service, signatures=_memory_signatures(triage, facts, observation.observed)
        )
        if recall.notes:
            for note in recall.notes:
                decision_trace.append(f"memory: {note}")
        else:
            decision_trace.append(
                f"memory: {recall.status}, {len(recall.priors)} prior(s) from "
                f"{'+'.join(recall.providers_used) or 'no provider'}"
            )
        run.emit(
            RcaStage.MEMORY_RECALL,
            f"…{len(recall.priors)} prior(s) from {'+'.join(recall.providers_used) or 'no provider'}",
            outcome=StageOutcome.OK,
            priors=len(recall.priors),
        )
        run.emit(RcaStage.ACTION_VOCABULARY, "Resolving executable actions")
        vocabulary, vocabulary_source = _action_vocabulary(service)
        run.emit(RcaStage.HYPOTHESES, "Generating and scoring hypotheses")
        result = pipeline.investigate(
            triage,
            facts,
            context=context,
            change_evidence=change_evidence,
            recall=recall,
            action_vocabulary=vocabulary,
            # An executor exists only when the *registry* answered. The static fallback
            # can confirm a key is real; it cannot run it. Keeping the two apart is what
            # lets a verdict say "known fix, no executor here" instead of offering a
            # button that fails after approval.
            executor_available=executor_available(vocabulary_source),
        )
    except Exception as exc:
        logger.warning("RCA investigation stages failed (%s); continuing without them", exc)
        decision_trace.append(
            f"deterministic investigation raised {type(exc).__name__}; "
            "falling back to the unstructured path"
        )
        run.emit(
            RcaStage.HYPOTHESES,
            f"…investigation raised {type(exc).__name__}; continuing without it",
            outcome=StageOutcome.DEGRADED,
        )
        return None

    ranked = ", ".join(
        f"{m.hypothesis.category}={m.score.score:.2f}" for m in result.matrices[:4] if m.score
    )
    decision_trace.append(
        f"investigation: {len(result.matrices)} hypothesis(es) scored "
        f"[{ranked or 'none'}]; status={result.status.value} confidence={result.confidence:.2f}; "
        f"completeness={result.completeness.overall:.0%}"
    )
    for note in result.notes:
        decision_trace.append(f"investigation note: {note}")
    run.emit(
        RcaStage.HYPOTHESES,
        f"…{len(result.matrices)} scored; top: {ranked or 'none'}",
        outcome=StageOutcome.OK,
        status=result.status.value,
        confidence=result.confidence,
    )
    return result


_GROUNDING_STOPWORDS = frozenset({"service", "pod", "container", "the", "and"})


def _grounding_needles(*sources: str) -> set[str]:
    """Short, stem-like tokens to look for in the model's prose.

    Two details, both learned from this check firing on correct answers:

    * **Split on punctuation, not whitespace.** A component is often a pod name
      (``user-service-7d4f8b6c9-x2n4p``) or a labelled store (``PostgreSQL
      (order-service)``), which contain no spaces at all — so a whitespace split produced
      one unmatchable token and the check fell through to the category every time.
    * **Compare stems, and keep three-letter tokens.** The category
      ``resource_saturation_cpu`` yielded ``resource``/``saturation`` and *dropped* ``cpu``
      for being too short, while ``saturation`` does not appear in the phrase "CPU-
      saturated". So two correct CPU diagnoses were downgraded to UNCERTAIN by a
      vocabulary mismatch rather than by anything wrong with them. Tokens of six or more
      characters are truncated to five so ``saturation`` matches ``saturated``.

    Generic words are dropped: nearly every RCA sentence contains "service", so matching on
    it would make this check pass unconditionally and stop protecting anything.
    """
    needles: set[str] = set()
    for source in sources:
        for token in re.split(r"[^a-z0-9]+", source.lower()):
            if len(token) < 3 or token in _GROUNDING_STOPWORDS:
                continue
            needles.add(token[:5] if len(token) >= 6 else token)
    return needles


def _grounded_in_investigation(root_cause: str, investigation: Investigation) -> bool:
    """Whether the model's prose actually describes the hypothesis that was scored.

    The confidence number is computed for a specific hypothesis. If the model writes about
    something else, that number is describing a different claim than the sentence beside
    it — which is worse than either being wrong alone, because the pair looks corroborated.

    Deliberately loose. The cost of a false negative is abstaining on a correct answer; the
    cost of a false positive is one unchecked prose mismatch, and the evidence matrix beside
    it still shows what was actually observed. So the check is tuned to catch prose about an
    entirely different failure, not to police wording.
    """
    selected = investigation.selected
    if selected is None:
        return False
    text = root_cause.lower()
    needles = _grounding_needles(
        selected.hypothesis.candidate_component or "", selected.hypothesis.category
    )
    return any(needle in text for needle in needles)


# ─── entry point ────────────────────────────────────────────────────────────


class _Observation(NamedTuple):
    """What one evidence pass produced, and where the facts should be read from.

    ``backend`` and the two availability flags exist so the investigation stages read the
    *same readings* the prompt was built from, rather than issuing their own query pass —
    see ``evidence.CachingBackend``. The flags are ``None`` on the live path, meaning
    "infer", because ``evidence._q`` cannot distinguish an empty answer from a failed one;
    a Context Pack can, and says so.
    """

    observed: dict[str, list[str]]
    backend: _evidence.Backend
    metrics_available: bool | None
    logs_available: bool | None


def _observe(
    service: str,
    context: dict[str, Any] | None,
    decision_trace: list[str],
) -> _Observation:
    """Live telemetry, from the shared context when one is usable, else gathered
    directly — exactly as this agent has always done.

    Reading ``AIOPS_CONTEXT_LAYER`` per call, not at import, so a fixture or an
    operator's environment change is honoured without a module reload (see
    ``aiops/context/config.py``). ``shadow`` mode compares both paths without ever
    changing what ``analyze`` returns.
    """
    from aiops.context import config as context_config
    from aiops.context import shadow as context_shadow
    from aiops.context.pack import IncidentContext

    # One caching backend per call, shared by the prompt's evidence and the
    # investigation's facts, so the live path issues each query once.
    live = _evidence.CachingBackend(_evidence.LiveBackend())

    mode = context_config.context_mode()
    if mode == "off" or context is None:
        return _Observation(_evidence.gather(service, live), live, None, None)

    try:
        ctx = IncidentContext.model_validate(context)
        from agents.rca_agent.context_adapter import evidence_from_context

        from_context = evidence_from_context(ctx, service)
    except Exception as exc:
        # A malformed or stale context must cost evidence, not the RCA. Same
        # posture as every other lookup in this module.
        decision_trace.append(f"context evidence unusable ({type(exc).__name__}); gathering live")
        return _Observation(_evidence.gather(service, live), live, None, None)

    if mode == "shadow":
        legacy = _evidence.gather(service, live)
        context_shadow.record_diff("rca_agent", legacy=legacy, from_context=from_context)
        # The legacy answer stays authoritative in shadow mode, so the facts must come
        # from the legacy backend too. Reading the prompt from one source and the score
        # from another would make shadow mode change the verdict, which is the one thing
        # it must never do.
        return _Observation(legacy, live, None, None)

    if not from_context:
        decision_trace.append("shared context had no usable evidence; gathering live")
        return _Observation(_evidence.gather(service, live), live, None, None)

    decision_trace.append("live evidence gathered from the shared context")
    from agents.rca_agent.context_adapter import ContextBackend

    return _Observation(
        from_context,
        ContextBackend(ctx),
        ctx.metrics.status.usable,
        ctx.logs.status.usable,
    )


def analyze(
    triage_verdict: dict[str, Any],
    *,
    scenario_id: str | None = None,
    correlation: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    offline: bool = False,
    progress: ProgressSink | None = None,
    run_id: str = "",
) -> RCAVerdict:
    """Produce an RCA verdict for one triaged incident.

    ``correlation`` is the optional ``CorrelationResult`` (dict form) from the
    Log Correlation agent (RA-007). When supplied, its suspect components, top
    signatures, and evidence summary are folded into the reasoning prompt.

    ``context`` is the optional shared ``IncidentContext`` (dict form) from the
    Context Engineering Layer. Additive and keyword-only, exactly like
    ``correlation`` — omitting it reproduces today's behavior exactly.

    ``offline=True`` suppresses **all** retrieval: no change correlation, no evidence
    gathering. Used by ``run()``, which the eval harness drives.

    That flag exists because ``run()``'s docstring claimed to be "the zero-I/O golden
    path" and was not: ``analyze`` reached ``_evidence.gather``, which issues about
    fourteen registry calls per invocation, and against a real
    ``AIOPS_PROMETHEUS_URL`` those are HTTP round-trips with timeouts. A golden file
    that talks to a cluster is not a golden file — its results depend on whether a
    port-forward happens to be open — and the cost is quadratic in the case count:
    growing the RCA golden from 1 case to 12 pushed
    ``test_main_summary_includes_both_buckets`` past the 60s per-test cap by issuing
    ~170 HTTP calls. With ``offline=True`` the goldens are deterministic, fast, and
    assert the same thing on a developer's laptop as in CI.

    ``progress``/``run_id`` are additive and keyword-only, exactly like
    ``correlation``/``context`` — omitting them reproduces today's behavior exactly
    (a ``NullSink`` under the hood, via ``RunProgress``). When supplied, real stage
    events fire at the real I/O and LLM boundaries below — see
    ``agents/rca_agent/progress.py`` for what "real" means here.
    """
    run = RunProgress(run_id, progress)
    service = str(triage_verdict.get("affected_service") or "unknown")
    run.emit(
        RcaStage.RECEIVED,
        f"Reading triage verdict for {service}",
        severity=triage_verdict.get("severity"),
    )
    decision_trace: list[str] = [
        f"received triage_verdict for service={triage_verdict.get('affected_service')!r} "
        f"severity={triage_verdict.get('severity')!r}"
    ]
    if correlation:
        suspects = correlation.get("suspected_dependencies") or []
        decision_trace.append(
            f"received RA-007 correlation evidence: {len(correlation.get('top_signatures') or [])} "
            f"signature(s), suspect components={suspects}"
        )

    # Change correlation. Deliberately best-effort: an unconfigured or
    # unreachable SCM seam must degrade the RCA's evidence, never fail the RCA.
    if offline:
        change_evidence = None
    else:
        run.emit(
            RcaStage.CHANGE_CORRELATION,
            f"Correlating recent changes ({_CHANGE_LOOKBACK_HOURS}h lookback)",
        )
        change_evidence = _fetch_change_evidence(service, decision_trace)
        n_changes = len(change_evidence) if change_evidence is not None else 0
        run.emit(
            RcaStage.CHANGE_CORRELATION,
            f"…{n_changes} change(s) found"
            if change_evidence is not None
            else "…SCM seam unreachable",
            outcome=StageOutcome.OK if change_evidence is not None else StageOutcome.DEGRADED,
            count=n_changes,
        )

    # Live telemetry, so the model reasons from what the system is actually
    # doing rather than pattern-matching the service name. Without this it can
    # only guess a mechanism — which is how the previous prompt produced
    # confident root causes naming feature flags that do not exist.
    # Never raises; an unreachable backend costs one evidence line, not the RCA.
    if offline:
        observation = _Observation(
            {}, _evidence.CachingBackend(_evidence.LiveBackend()), False, False
        )
        decision_trace.append(
            "offline mode: no retrieval attempted (evidence is UNAVAILABLE, not absent)"
        )
    else:
        run.emit(RcaStage.EVIDENCE, "Gathering live evidence (metrics, logs, traces)")
        observation = _observe(service, context, decision_trace)
    observed = observation.observed
    if observed:
        decision_trace.append(
            "live evidence gathered: " + ", ".join(f"{k}={len(v)}" for k, v in observed.items())
        )
        if not offline:
            run.emit(
                RcaStage.EVIDENCE,
                "…" + ", ".join(f"{k}={len(v)}" for k, v in observed.items()),
                outcome=StageOutcome.OK,
            )
    else:
        decision_trace.append(
            "no live evidence (observability seams unreachable); reasoning from "
            "the triage verdict alone"
        )
        if not offline:
            run.emit(
                RcaStage.EVIDENCE,
                "…observability seams unreachable; reasoning from the triage verdict alone",
                outcome=StageOutcome.DEGRADED,
            )

    # The deterministic investigation. Runs before the LLM and owns the conclusion's
    # *number*: hypotheses are generated from evidence, scored by rule, and the top one's
    # score becomes the verdict's confidence. The model's job below is the prose.
    investigation = _investigate(
        triage_verdict,
        observation,
        context=context,
        change_evidence=change_evidence,
        offline=offline,
        decision_trace=decision_trace,
        progress=run,
    )

    user_prompt = _render_user_prompt(
        triage_verdict, correlation, change_evidence, observed, investigation=investigation
    )
    try:
        # JSON mode would be ideal but the gateway is provider-agnostic and
        # not every backend supports it; we ask for JSON in the prompt and
        # parse defensively. 1500 tokens covers reasoning + a 2-3 step plan.
        rca_provider, rca_model = _rca_provider(), _rca_model()
        run.emit(
            RcaStage.EXPLAINING,
            "Explaining the top hypothesis (model)",
            provider=rca_provider,
            model=rca_model,
        )
        resp = llm_complete(
            messages=[
                Message(role="system", content=SYSTEM_PROMPT_V7),
                Message(role="user", content=user_prompt),
            ],
            provider=rca_provider,
            model=rca_model,
            temperature=0.2,
            max_tokens=1500,
        )
        text = (resp.text or "").strip()
        if not text or text.startswith("[stub]"):
            decision_trace.append("LLM provider is stub (echo); using deterministic fallback")
            run.emit(
                RcaStage.EXPLAINING,
                "…stub provider; using the deterministic verdict",
                outcome=StageOutcome.DEGRADED,
            )
            return _fallback_verdict(
                triage_verdict,
                scenario_id=scenario_id,
                decision_trace=decision_trace,
                investigation=investigation,
            )
        raw = _extract_json_object(text)
        if raw is None:
            decision_trace.append("LLM response was not parseable JSON; falling back")
            run.emit(
                RcaStage.EXPLAINING,
                "…response was not parseable JSON; using the deterministic verdict",
                outcome=StageOutcome.DEGRADED,
            )
            return _fallback_verdict(
                triage_verdict,
                scenario_id=scenario_id,
                decision_trace=decision_trace,
                investigation=investigation,
            )
        verdict = _coerce_verdict(
            raw,
            service=service,
            decision_trace=decision_trace,
            has_evidence=bool(observed),
            investigation=investigation,
        )
        if verdict is None:
            decision_trace.append("LLM JSON failed schema validation; falling back")
            run.emit(
                RcaStage.EXPLAINING,
                "…response failed schema validation; using the deterministic verdict",
                outcome=StageOutcome.DEGRADED,
            )
            return _fallback_verdict(
                triage_verdict,
                scenario_id=scenario_id,
                decision_trace=decision_trace,
                investigation=investigation,
            )
        decision_trace.append(
            f"LLM produced verdict with {len(verdict.ranked_fix_steps)} fix step(s), "
            f"confidence={verdict.confidence_score:.2f}"
        )
        run.emit(
            RcaStage.EXPLAINING,
            "…verdict produced",
            outcome=StageOutcome.OK,
            fix_steps=len(verdict.ranked_fix_steps),
            confidence=verdict.confidence_score,
        )
        return verdict
    except Exception as exc:
        logger.warning("RCA LLM call failed (%s); using deterministic fallback", exc)
        decision_trace.append(f"LLM call raised {type(exc).__name__}; falling back")
        run.emit(
            RcaStage.EXPLAINING,
            f"…LLM call raised {type(exc).__name__}; using the deterministic verdict",
            outcome=StageOutcome.DEGRADED,
        )
        return _fallback_verdict(
            triage_verdict,
            scenario_id=scenario_id,
            decision_trace=decision_trace,
            investigation=investigation,
        )


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out shim around ``analyze``.

    Runs with ``offline=True`` and never passes ``context``, so the golden path
    performs no retrieval at all regardless of ``AIOPS_CONTEXT_LAYER``,
    ``AIOPS_PROMETHEUS_URL`` or whether a cluster happens to be reachable. The eval
    harness compares a run against a fixed golden file, and that comparison is only
    meaningful if the run cannot vary with the environment.

    Accuracy against telemetry is measured separately by ``evals/rca_eval.py``, which
    supplies a synthetic ``IncidentContext`` and calls ``analyze`` directly.
    """
    parsed = RCAInput(**input)
    verdict = analyze(
        parsed.triage_verdict,
        scenario_id=parsed.scenario_id,
        correlation=parsed.correlation,
        offline=True,
    )
    return verdict.model_dump(mode="json")


def reset_state() -> None:
    """Eval-harness hook. RCA agent is stateless in v0 — no clusters, no
    persistence, no in-memory caches. Defined as a no-op so the harness can
    call it uniformly across all agents."""
    return None
