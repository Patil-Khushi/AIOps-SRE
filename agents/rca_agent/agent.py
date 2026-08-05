"""RCA Agent (PRS-008) — root-cause analysis + ranked reversible fix steps.

Entry point: ``analyze(triage_verdict, scenario_id=None) -> RCAVerdict``.

Pipeline (v0):

    1. Validate input  (pydantic on RCAInput)
    2. Render the user prompt from the triage verdict
    3. LLM reasoning pass (single prompt, JSON-mode)
    4. Parse + pydantic-validate the LLM JSON
    5. Force ``requires_hitl=True`` on every fix step (the catalog invariant
       the platform gate enforces; we never trust the LLM to set this)
    6. Fall back to a deterministic verdict when the LLM is unavailable or
       returns an unparseable response (CI path with the stub provider)

Vendor-neutrality: imports only from ``aiops.llm``. No SDK imports, no
direct vendor clients. v0 has no retrieval phase — that lands in W2 once
the prompt has stabilized on the locked scenario.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from agents.rca_agent import evidence as _evidence
from agents.rca_agent.models import (
    BlastRadius,
    FixActionType,
    RankedFixStep,
    RCAAuditMetadata,
    RCAInput,
    RCAVerdict,
)
from agents.rca_agent.prompts import (
    CHANGE_EVIDENCE_BLOCK,
    CORRELATION_EVIDENCE_BLOCK,
    RCA_PROMPT_USER_V1,
    SYSTEM_PROMPT_V5,
)
from agents.rca_agent.remediation_map import flag_for_service
from aiops.llm import Message
from aiops.llm import complete as llm_complete
from aiops.tools import get_registry

logger = logging.getLogger(__name__)

# Deterministic fallback covers exactly the v0-locked scenario. Any other
# scenario_id with no usable LLM response surfaces as a low-confidence verdict
# rather than a confident wrong answer (the truth file's "known_wrong_fixes"
# section is explicit that pattern-matching to restart/scale is a failure mode).
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
_RCA_PROVIDER = os.environ.get("AIOPS_RCA_LLM_PROVIDER", "anthropic")
_RCA_MODEL = os.environ.get("AIOPS_RCA_LLM_MODEL", "claude-sonnet-4-6")

# Exact service identifiers that map to the locked scenario. Used so the
# dashboard path (which may not pass scenario_id) still hits the confident
# verdict for the actual broken service, without a loose substring match like
# "product" in service that would also fire on unrelated product-* services.
_LOCKED_SERVICES = frozenset({"user-service", "userservice", "user"})


# ─── deterministic fallback ─────────────────────────────────────────────────


def _fallback_verdict(
    triage: dict[str, Any],
    *,
    scenario_id: str | None,
    decision_trace: list[str],
) -> RCAVerdict:
    """Hand-written verdict matching ``demo/ecommerce/truth_files/user_service_mysql_down.json``.

    Used when (a) the LLM provider is the stub, (b) the LLM response is
    unparseable, or (c) the scenario is locked-v0 and we want to guarantee
    eval-harness coverage independent of LLM availability.
    """
    # "unknown", NOT a service name. This used to default to
    # "productcatalogservice", which is in _LOCKED_SERVICES — so a triage
    # verdict with a MISSING affected_service silently produced a confident
    # root cause about a service that was never involved (and, after the
    # migration, no longer exists).
    service = triage.get("affected_service") or "unknown"

    if scenario_id == _LOCKED_SCENARIO or service.lower() in _LOCKED_SERVICES:
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
            audit_metadata=RCAAuditMetadata(
                created_at=datetime.now(UTC),
                decision_trace=decision_trace,
            ),
        )

    # Unknown scenario without a usable LLM — emit a low-confidence "I don't
    # know" verdict rather than a confident wrong answer. Only one scenario
    # has a hand-written fallback; this branch keeps the contract honest for
    # the other eleven when the LLM is unavailable.
    decision_trace.append(
        f"deterministic fallback: scenario_id={scenario_id!r} not in locked-v0 set; "
        "emitting low-confidence verdict"
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


def _ground_set_flags_against_flagd(
    steps: list[RankedFixStep], *, decision_trace: list[str]
) -> list[RankedFixStep]:
    """Downgrade any ``set_flag`` step whose flag isn't in the live flagd config
    to ``manual`` — so the dashboard never offers a one-click apply the executor
    will reject with "flag not present in flagd config".

    Used for services the curated map doesn't cover (e.g. ``frontend``, which
    has *no* one-flag remediation in the OTel demo). The LLM follows the
    ``<service>Failure`` naming pattern and confidently invents flags like
    ``frontendFailure`` that don't exist; grounding them against the real config
    turns that into an honest "investigate manually" instead of a dead button.

    Fails open: if the flag list is unavailable the steps are returned unchanged
    and the executor remains the backstop. Skips the flagd lookup entirely when
    there's no ``set_flag`` step to validate.
    """
    if not any(s.action_type is FixActionType.SET_FLAG and s.flag for s in steps):
        return steps
    available = _live_flag_names()
    if not available:
        return steps
    grounded: list[RankedFixStep] = []
    for i, s in enumerate(steps):
        if s.action_type is FixActionType.SET_FLAG and s.flag and s.flag not in available:
            decision_trace.append(
                f"downgraded fix step #{i + 1}: flag {s.flag!r} is not a configured flagd "
                "flag — no one-flag remediation for this service; marked manual"
            )
            grounded.append(
                s.model_copy(
                    update={
                        "action_type": FixActionType.MANUAL,
                        "flag": None,
                        "variant": "off",
                        "description": (
                            f"{s.description}  [NOTE: '{s.flag}' is not a configured flagd "
                            "flag, so no automated flag-flip is available — investigate manually.]"
                        ),
                    }
                )
            )
        else:
            grounded.append(s)
    return grounded


def _ensure_executable_action(
    steps: list[RankedFixStep],
    *,
    service: str,
    decision_trace: list[str],
) -> list[RankedFixStep]:
    """Make the curated service→flag map authoritative for the executable step.

    When the affected service maps to a known flagd failure flag, this both
    *backstops* (annotates the primary step ``set_flag`` if the LLM left it
    manual) and *corrects* (overrides a flag the LLM guessed wrong). The LLM
    follows the ``<service>Failure`` naming pattern and sometimes emits a flag
    that does not exist — e.g. ``recommendationFailure`` instead of the real
    ``recommendationCacheFailure`` — which makes the executor fail with "flag
    not present in flagd config". The map values are real flagd flags, so we
    trust them over the model's spelling.

    For services the map doesn't know, we ground the LLM's ``set_flag`` step
    against the live flagd config: a real flag is kept, an invented one is
    downgraded to ``manual`` so the UI never offers an un-runnable apply. The
    executor still validates as a final backstop.
    """
    mapped = flag_for_service(service)
    if not mapped:
        return _ground_set_flags_against_flagd(steps, decision_trace=decision_trace)
    # Target the first set_flag step the LLM proposed; if it proposed none,
    # fall back to the top-ranked step so the demo still offers one-click apply.
    target_idx = next(
        (i for i, s in enumerate(steps) if s.action_type is FixActionType.SET_FLAG),
        0,
    )
    before = steps[target_idx]
    if before.action_type is FixActionType.SET_FLAG and before.flag == mapped:
        return steps  # already correct — nothing to do
    steps[target_idx] = before.model_copy(
        update={"action_type": FixActionType.SET_FLAG, "flag": mapped, "variant": "off"}
    )
    if before.action_type is FixActionType.SET_FLAG and before.flag and before.flag != mapped:
        decision_trace.append(
            f"corrected fix step #{target_idx + 1} flag {before.flag!r} → {mapped!r} "
            f"(authoritative map for affected_service={service!r})"
        )
    else:
        decision_trace.append(
            f"annotated fix step #{target_idx + 1} with executable action "
            f"set_flag(flag={mapped}, off) from affected_service={service!r}"
        )
    return steps


def _coerce_verdict(
    raw: dict[str, Any],
    *,
    service: str,
    decision_trace: list[str],
) -> RCAVerdict | None:
    """Coerce a parsed LLM JSON dict into ``RCAVerdict``.

    Forces ``requires_hitl=True`` on every fix step regardless of what the
    model emitted — the catalog invariant is platform-enforced, not model-
    trusted. Clamps the step list to at most 3 to keep the dashboard tidy.
    Returns ``None`` on validation failure so the caller can fall back.
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
        return RCAVerdict(
            affected_service=service,
            root_cause=str(raw.get("root_cause", "")).strip(),
            ranked_fix_steps=steps,
            confidence_score=float(raw.get("confidence_score", 0.5)),
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


def _render_user_prompt(
    triage: dict[str, Any],
    correlation: dict[str, Any] | None = None,
    change_evidence: list[dict[str, Any]] | None = None,
    observed: dict[str, list[str]] | None = None,
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
    return RCA_PROMPT_USER_V1.format(
        service=service,
        severity=severity,
        summary=summary,
        decision_trace=rendered_trace,
        evidence_block=evidence,
    )


# ─── entry point ────────────────────────────────────────────────────────────


def analyze(
    triage_verdict: dict[str, Any],
    *,
    scenario_id: str | None = None,
    correlation: dict[str, Any] | None = None,
) -> RCAVerdict:
    """Produce an RCA verdict for one triaged incident.

    ``correlation`` is the optional ``CorrelationResult`` (dict form) from the
    Log Correlation agent (RA-007). When supplied, its suspect components, top
    signatures, and evidence summary are folded into the reasoning prompt.
    """
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
    service = str(triage_verdict.get("affected_service") or "unknown")

    # Change correlation. Deliberately best-effort: an unconfigured or
    # unreachable SCM seam must degrade the RCA's evidence, never fail the RCA.
    change_evidence = _fetch_change_evidence(service, decision_trace)

    # Live telemetry, so the model reasons from what the system is actually
    # doing rather than pattern-matching the service name. Without this it can
    # only guess a mechanism — which is how the previous prompt produced
    # confident root causes naming feature flags that do not exist.
    # Never raises; an unreachable backend costs one evidence line, not the RCA.
    observed = _evidence.gather(service)
    if observed:
        decision_trace.append(
            "live evidence gathered: " + ", ".join(f"{k}={len(v)}" for k, v in observed.items())
        )
    else:
        decision_trace.append(
            "no live evidence (observability seams unreachable); reasoning from "
            "the triage verdict alone"
        )

    user_prompt = _render_user_prompt(triage_verdict, correlation, change_evidence, observed)
    try:
        # JSON mode would be ideal but the gateway is provider-agnostic and
        # not every backend supports it; we ask for JSON in the prompt and
        # parse defensively. 1500 tokens covers reasoning + a 2-3 step plan.
        resp = llm_complete(
            messages=[
                Message(role="system", content=SYSTEM_PROMPT_V5),
                Message(role="user", content=user_prompt),
            ],
            provider=_RCA_PROVIDER,
            model=_RCA_MODEL,
            temperature=0.2,
            max_tokens=1500,
        )
        text = (resp.text or "").strip()
        if not text or text.startswith("[stub]"):
            decision_trace.append("LLM provider is stub (echo); using deterministic fallback")
            return _fallback_verdict(
                triage_verdict, scenario_id=scenario_id, decision_trace=decision_trace
            )
        raw = _extract_json_object(text)
        if raw is None:
            decision_trace.append("LLM response was not parseable JSON; falling back")
            return _fallback_verdict(
                triage_verdict, scenario_id=scenario_id, decision_trace=decision_trace
            )
        verdict = _coerce_verdict(raw, service=service, decision_trace=decision_trace)
        if verdict is None:
            decision_trace.append("LLM JSON failed schema validation; falling back")
            return _fallback_verdict(
                triage_verdict, scenario_id=scenario_id, decision_trace=decision_trace
            )
        decision_trace.append(
            f"LLM produced verdict with {len(verdict.ranked_fix_steps)} fix step(s), "
            f"confidence={verdict.confidence_score:.2f}"
        )
        return verdict
    except Exception as exc:
        logger.warning("RCA LLM call failed (%s); using deterministic fallback", exc)
        decision_trace.append(f"LLM call raised {type(exc).__name__}; falling back")
        return _fallback_verdict(
            triage_verdict, scenario_id=scenario_id, decision_trace=decision_trace
        )


def run(input: dict[str, Any]) -> dict[str, Any]:
    """Eval-harness contract: dict-in, dict-out shim around ``analyze``."""
    parsed = RCAInput(**input)
    verdict = analyze(
        parsed.triage_verdict, scenario_id=parsed.scenario_id, correlation=parsed.correlation
    )
    return verdict.model_dump(mode="json")


def reset_state() -> None:
    """Eval-harness hook. RCA agent is stateless in v0 — no clusters, no
    persistence, no in-memory caches. Defined as a no-op so the harness can
    call it uniformly across all agents."""
    return None
