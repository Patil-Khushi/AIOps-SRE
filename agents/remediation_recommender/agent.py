"""Remediation Recommender (PRS-001) — Day-1 scaffold.

Sits between the RCA Agent (PRS-008) and Auto-Healer / ``auto_healer_lite``
(PRS-002). Consumes an ``RCAVerdict`` and emits a ``RemediationVerdict``
containing a ranked decision set of remediation options. The operator
picks one (HITL Optional); Auto-Healer executes the chosen option
through the existing platform HITL gate (HITL Required, enforced at the
tool boundary — CLAUDE.md #3).

Day-1 contract (this file):

- ``run(input)`` is deterministic — no LLM call. The stub:
    1. Pulls options 1:1 from ``rca_verdict.ranked_fix_steps`` (source=
       ``rca_fix_step``), preserving RCA's order as a tie-breaker.
    2. Adds catalog-driven mitigations matching the root_cause text
       (source=``playbook_pattern``).
    3. Re-ranks the union by a transparent composite score:
       ``score = (6 - blast_radius_score) * 10 + confidence * 5
                 + rollback_tested_bonus + env_preference_bonus``.
       Lower blast radius wins ties.
    4. Marks ``recommended_option_id`` = options[0].option_id (the top
       of the sorted list). ``auto_pick_eligible`` stays False — every
       option still flows through the HITL gate downstream.

What this file does NOT do (deferred to v1):

- No LLM-driven re-ranking based on free-text rationale matching.
- No historical-effectiveness pull from ``HistoricalIncidentRow``.
- No cost-aware ordering (scaling out vs flag-flip dollar cost).
- No execution — Auto-Healer owns the tool call after operator approval.

Test surface:

- ``run(input: dict) -> dict`` matches the eval-harness convention.
  Inputs are dict-form (RCAInput-compatible); output is the JSON-mode
  dump of ``RemediationVerdict``.
- ``recommend(input: RemediationInput) -> RemediationVerdict`` is the
  typed entry-point for code-level callers (Auto-Healer once it lands).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .models import (
    ActionType,
    BlastRadius,
    OptionSource,
    RecoAuditMetadata,
    RemediationInput,
    RemediationOption,
    RemediationVerdict,
    blast_radius_score,
)
from .remediation_catalog import CatalogOption, patterns_for_cause

logger = logging.getLogger(__name__)

# Map ``RankedFixStep.action_type`` (string) → ``ActionType`` enum.
# Anything unknown collapses to ``manual`` so an LLM emitting a
# future action type doesn't break ingestion.
_RCA_ACTION_TYPE_MAP: dict[str, ActionType] = {
    "set_flag": ActionType.SET_FLAG,
    "rollback_deploy": ActionType.ROLLBACK_DEPLOY,
    "manual": ActionType.MANUAL,
}

# MTTR median by blast radius — rough catalog priors. Used only when the
# RCA fix step doesn't carry its own MTTR estimate (current RCAVerdict
# shape doesn't carry one; v1 may).
_MTTR_BY_BLAST_RADIUS: dict[BlastRadius, int] = {
    BlastRadius.LOW: 3,
    BlastRadius.MEDIUM: 10,
    BlastRadius.HIGH: 30,
}

# Tool capability inferred from action type. ``manual`` actions and
# action types without a wired executor leave the field None — Auto-
# Healer surfaces those as instructions rather than firing them.
_TOOL_CAPABILITY_BY_ACTION: dict[ActionType, str | None] = {
    ActionType.SET_FLAG: "automation.fault.clear",
    ActionType.ROLLBACK_DEPLOY: "k8s.deployment.rollback",
    ActionType.SCALE: "k8s.deployment.scale",
    ActionType.RESTART: "k8s.deployment.restart",
    ActionType.CIRCUIT_BREAKER: "automation.fault.clear",
    ActionType.MANUAL: None,
}


# ─── Conversion: RCA fix step → RemediationOption ─────────────────────────


def _option_from_rca_step(
    *,
    affected_service: str,
    step: dict[str, Any],
    rca_confidence: float,
    rank_index: int,
    total_steps: int,
) -> RemediationOption:
    """Convert one RCA ``RankedFixStep`` (dict-form) into an option.

    Confidence is decayed slightly for steps further down RCA's list
    (RCA already ordered them — the first is most likely to fix the
    diagnosed cause). The decay is shallow so a low-blast-radius
    fallback option doesn't get buried under a high-blast-radius top
    pick once we re-rank.
    """
    blast_str = str(step.get("blast_radius", "medium")).lower()
    blast = (
        BlastRadius(blast_str)
        if blast_str in BlastRadius.__members__.values()
        else BlastRadius.MEDIUM
    )

    action_str = str(step.get("action_type", "manual")).lower()
    action = _RCA_ACTION_TYPE_MAP.get(action_str, ActionType.MANUAL)

    # Build executor args for the two RCA action types that carry them.
    tool_args: dict[str, Any] = {}
    if action == ActionType.SET_FLAG:
        flag = step.get("flag")
        if flag:
            tool_args = {"flag": flag, "variant": step.get("variant", "off")}

    rank_decay = 0.05 * rank_index  # 0, 0.05, 0.10, ...
    confidence = max(0.0, min(1.0, rca_confidence - rank_decay))

    description = str(step.get("description") or "").strip()
    if not description:
        description = f"RCA-proposed fix step {rank_index + 1} of {total_steps}"

    rollback = str(step.get("rollback") or "").strip() or "Rollback not specified by RCA."

    return RemediationOption(
        option_id=f"rca-step-{rank_index + 1}",
        title=description[:80],
        description=description,
        action_type=action,
        blast_radius=blast,
        blast_radius_score=blast_radius_score(blast),
        rollback=rollback,
        rollback_tested=action == ActionType.SET_FLAG,  # flag flips are atomic
        confidence=confidence,
        estimated_mttr_minutes=_MTTR_BY_BLAST_RADIUS[blast],
        rationale=f"From RCA: ranked #{rank_index + 1} of {total_steps} fix steps for the diagnosed cause.",
        tool_capability=_TOOL_CAPABILITY_BY_ACTION[action],
        tool_args=tool_args,
        source=OptionSource.RCA_FIX_STEP,
    )


# ─── Conversion: catalog pattern → RemediationOption ──────────────────────


def _option_from_catalog(
    *,
    affected_service: str,
    template: CatalogOption,
) -> RemediationOption:
    """Render a :class:`CatalogOption` template into a concrete option.

    Templated ``tool_args`` placeholders (``"{service}"``) are filled
    from the affected service. Any failure to render falls back to an
    empty args dict — the executor will then surface the option as a
    manual instruction rather than firing it.
    """
    tool_args: dict[str, Any] = {}
    if template.tool_args_template:
        try:
            tool_args = {
                k: v.format(service=affected_service) if isinstance(v, str) else v
                for k, v in template.tool_args_template.items()
            }
        except (KeyError, ValueError) as exc:
            logger.warning(
                "catalog option %r: failed to render tool_args template (%s); "
                "falling back to manual",
                template.option_id,
                exc,
            )

    return RemediationOption(
        option_id=template.option_id,
        title=template.title,
        description=template.description,
        action_type=template.action_type,
        blast_radius=template.blast_radius,
        blast_radius_score=blast_radius_score(template.blast_radius),
        rollback=template.rollback,
        rollback_tested=template.rollback_tested,
        confidence=template.confidence,
        estimated_mttr_minutes=template.estimated_mttr_minutes,
        rationale=template.rationale,
        tool_capability=template.tool_capability,
        tool_args=tool_args,
        source=OptionSource.PLAYBOOK_PATTERN,
    )


# ─── Scoring + ranking ────────────────────────────────────────────────────


def _composite_score(
    option: RemediationOption,
    *,
    environment: str,
    prefer_safe: bool,
) -> float:
    """Higher score = better rank. Components are explained inline.

    Day-1 weighting (transparent, no LLM): prefer safer blast radius
    over slightly-higher confidence, then break ties on rollback proof.

    Production gets an extra safety boost — staging/dev tolerates
    higher blast radius because the cost of a wrong move is lower.
    """
    # Safer blast radius dominates: a low (score=1) gets 50 points; a
    # high (score=5) gets 10. This out-weighs the 5 points of full
    # confidence, on purpose — Day-1 stub leans into "first, do no
    # harm" because nothing is auto-executed without HITL anyway.
    blast_component = (6 - option.blast_radius_score) * 10

    confidence_component = option.confidence * 5
    rollback_bonus = 3 if option.rollback_tested else 0

    env_bonus = 0
    if environment == "production" and prefer_safe and option.blast_radius == BlastRadius.LOW:
        env_bonus = 5
    # Below-prod environments: tilt slightly toward bolder options so the
    # developer can validate higher-blast moves cheaply.
    elif environment in ("staging", "dev") and option.blast_radius == BlastRadius.MEDIUM:
        env_bonus = 2

    return blast_component + confidence_component + rollback_bonus + env_bonus


# ─── Public surface ───────────────────────────────────────────────────────


def recommend(input_payload: RemediationInput) -> RemediationVerdict:
    """Typed entry-point. Pure — no I/O, no LLM, no side effects.

    Builds the option list, scores it, sorts, and packages a
    :class:`RemediationVerdict`. Auto-Healer (when it lands) calls this
    + then routes the chosen option through ``aiops.tools.get_registry()
    .call(option.tool_capability, **option.tool_args)`` — but only after
    the platform HITL gate clears, NOT from inside this function.
    """
    trace: list[str] = []

    rca = input_payload.rca_verdict
    affected_service = str(rca.get("affected_service") or "unknown")
    root_cause = str(rca.get("root_cause") or "").strip()
    rca_confidence = float(rca.get("confidence_score") or 0.5)
    fix_steps = rca.get("ranked_fix_steps") or []

    trace.append(
        f"input: service={affected_service!r} root_cause={root_cause[:60]!r} "
        f"rca_confidence={rca_confidence:.2f} fix_steps={len(fix_steps)}"
    )

    options: list[RemediationOption] = []
    seen_ids: set[str] = set()

    # 1. RCA fix steps → 1:1 options.
    for idx, step in enumerate(fix_steps):
        if not isinstance(step, dict):
            continue
        try:
            opt = _option_from_rca_step(
                affected_service=affected_service,
                step=step,
                rca_confidence=rca_confidence,
                rank_index=idx,
                total_steps=len(fix_steps),
            )
        except Exception as exc:  # boundary: skip malformed steps
            logger.warning("skipping malformed RCA step #%d (%s)", idx, exc)
            continue
        if opt.option_id in seen_ids:
            continue
        options.append(opt)
        seen_ids.add(opt.option_id)
    trace.append(
        f"rca-derived options: {sum(1 for o in options if o.source == OptionSource.RCA_FIX_STEP)}"
    )

    # 2. Catalog patterns → symptom-driven mitigations RCA may have missed.
    catalog_hits = patterns_for_cause(root_cause)
    for template in catalog_hits:
        opt = _option_from_catalog(affected_service=affected_service, template=template)
        if opt.option_id in seen_ids:
            continue
        options.append(opt)
        seen_ids.add(opt.option_id)
    trace.append(
        f"catalog-derived options: {sum(1 for o in options if o.source == OptionSource.PLAYBOOK_PATTERN)} "
        f"(patterns_matched={len(catalog_hits)})"
    )

    # Defensive: if neither RCA nor the catalog yielded an option, emit
    # a single "investigate manually" stub so the verdict still satisfies
    # ``options: min_length=1``. Indicates a gap we should learn from.
    if not options:
        trace.append("no options from RCA or catalog — emitting manual placeholder")
        options.append(
            RemediationOption(
                option_id="manual-investigate",
                title="Investigate manually",
                description=(
                    f"No automated remediation found for cause {root_cause!r}. "
                    "Page the on-call engineer and follow the team runbook."
                ),
                action_type=ActionType.MANUAL,
                blast_radius=BlastRadius.LOW,
                blast_radius_score=blast_radius_score(BlastRadius.LOW),
                rollback="N/A — human-driven action.",
                rollback_tested=False,
                confidence=0.3,
                estimated_mttr_minutes=30,
                rationale="No RCA fix step and no catalog pattern matched.",
                tool_capability=None,
                tool_args={},
                source=OptionSource.OPERATOR_SEEDED,
            )
        )

    # 3. Rank.
    prefer_safe = bool(input_payload.operator_preferences.get("prefer_safe", True))
    environment = input_payload.environment
    options.sort(
        key=lambda o: (
            -_composite_score(o, environment=environment, prefer_safe=prefer_safe),
            o.blast_radius_score,
            -o.confidence,
            o.option_id,  # stable tie-break for determinism
        )
    )
    trace.append(
        f"ranked: top={options[0].option_id!r} "
        f"score={_composite_score(options[0], environment=environment, prefer_safe=prefer_safe):.1f}"
    )

    # 4. Verdict-level confidence: mean of top-3 options' confidence.
    top_confs = [o.confidence for o in options[:3]]
    overall_confidence = sum(top_confs) / len(top_confs) if top_confs else 0.0

    triage = input_payload.triage_verdict or {}
    incident_summary = (
        str(triage.get("alert_summary"))
        if triage.get("alert_summary")
        else f"Remediation for {root_cause or 'unknown cause'} on {affected_service}"
    )

    rationale = (
        f"Top pick is {options[0].title!r} "
        f"(blast={options[0].blast_radius.value}, "
        f"confidence={options[0].confidence:.2f}). "
        f"{len(options)} option(s) ranked. Execution gated by platform HITL — "
        f"Auto-Healer will not fire until an operator approves."
    )

    return RemediationVerdict(
        affected_service=affected_service,
        incident_summary=incident_summary,
        options=options,
        recommended_option_id=options[0].option_id,
        confidence_score=overall_confidence,
        rationale=rationale,
        audit_metadata=RecoAuditMetadata(
            created_at=datetime.now(UTC),
            decision_trace=trace,
        ),
    )


def run(input_payload: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness entry point.

    Accepts a dict shaped like ``RemediationInput`` and returns the JSON-
    serialisable form of the resulting verdict. Matches the convention
    all other agents in this repo follow (see ``evals/harness.py``).
    """
    typed = RemediationInput.model_validate(input_payload)
    verdict = recommend(typed)
    return verdict.model_dump(mode="json")


__all__ = ["recommend", "run"]
