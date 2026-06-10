"""RCA fix-step execution contract (RCA → approve → remediate).

Covers the seam that was previously disconnected: the platform executor now
*follows* the machine-readable action the RCA agent annotates on a fix step,
instead of a UI re-deriving the flag from a hardcoded service map.

These tests stay off the cluster: they exercise the agent's step annotation
and the executor's dispatch for the non-flag actions (which need no seam),
plus the no-flag guard. The full approve→execute happy path is covered by
``tests/test_hitl_approval_flow.py`` against a fake tool.
"""

from __future__ import annotations

from agents.rca_agent.agent import _coerce_action, _ensure_executable_action, _fallback_verdict
from agents.rca_agent.models import BlastRadius, FixActionType, RankedFixStep
from aiops.tools.rca_remediation import execute_rca_fix_step

# ─── agent annotates an executable action on the fix step ──────────────────


def test_locked_scenario_fallback_annotates_set_flag_action():
    """The deterministic verdict for slow-product-catalog must carry an
    executable set_flag action so the executor can follow it directly.

    Tests ``_fallback_verdict`` directly rather than ``analyze`` — the latter
    calls the live LLM when credentials are present, which is non-deterministic
    and would make this assertion flaky. The deterministic verdict is the
    contract we control."""
    verdict = _fallback_verdict(
        {"affected_service": "product-catalog", "severity": "Sev-2"},
        scenario_id="slow-product-catalog",
        decision_trace=[],
    )
    primary = verdict.ranked_fix_steps[0]
    assert primary.action_type is FixActionType.SET_FLAG
    assert primary.flag == "productCatalogFailure"
    assert primary.variant == "off"
    # The deploy-rollback step is recognised but has no executor — manual-ish.
    assert verdict.ranked_fix_steps[1].action_type is FixActionType.ROLLBACK_DEPLOY


def test_unknown_scenario_fallback_step_is_manual():
    """A low-confidence 'I don't know' verdict must not claim an executable
    action — it stays manual so the UI offers no one-click apply."""
    verdict = _fallback_verdict(
        {"affected_service": "mystery-svc", "severity": "Sev-3"},
        scenario_id="not-a-real-scenario",
        decision_trace=[],
    )
    assert all(s.action_type is FixActionType.MANUAL for s in verdict.ranked_fix_steps)


# ─── LLM action coercion is defensive ──────────────────────────────────────


def test_coerce_action_parses_valid_set_flag():
    action_type, flag, variant = _coerce_action(
        {"action_type": "set_flag", "flag": "paymentFailure", "variant": "off"}
    )
    assert (action_type, flag, variant) == (FixActionType.SET_FLAG, "paymentFailure", "off")


def test_coerce_action_downgrades_set_flag_without_flag_to_manual():
    action_type, flag, _ = _coerce_action({"action_type": "set_flag"})
    assert action_type is FixActionType.MANUAL
    assert flag is None


def test_coerce_action_unknown_type_becomes_manual():
    action_type, flag, _ = _coerce_action({"action_type": "rm -rf /", "flag": "x"})
    assert action_type is FixActionType.MANUAL
    # flag is dropped for non-set_flag actions.
    assert flag is None


def test_ensure_executable_action_backstops_from_service_map():
    """If the LLM left every step manual but the service maps to a known flag,
    the primary step is annotated set_flag — preserving the apply button."""
    steps = [
        RankedFixStep(
            description="restart the pod",
            blast_radius=BlastRadius.LOW,
            rollback="n/a",
        )
    ]
    trace: list[str] = []
    out = _ensure_executable_action(steps, service="cartservice", decision_trace=trace)
    assert out[0].action_type is FixActionType.SET_FLAG
    assert out[0].flag == "cartFailure"
    assert trace  # the backstop is recorded in the decision trace


def test_ensure_executable_action_corrects_hallucinated_flag():
    """The LLM sometimes guesses a flag that follows the <service>Failure
    pattern but isn't real (e.g. 'recommendationFailure' vs the actual
    'recommendationCacheFailure'). The curated map must override it so the
    executor flips a flag that exists in flagd."""
    steps = [
        RankedFixStep(
            description="Disable the recommendationFailure flag",
            blast_radius=BlastRadius.LOW,
            rollback="re-enable",
            action_type=FixActionType.SET_FLAG,
            flag="recommendationFailure",  # does not exist in flagd
        )
    ]
    trace: list[str] = []
    out = _ensure_executable_action(steps, service="recommendation", decision_trace=trace)
    assert out[0].flag == "recommendationCacheFailure"
    assert any("corrected" in line for line in trace)


def test_ensure_executable_action_leaves_correct_flag_untouched():
    steps = [
        RankedFixStep(
            description="Disable paymentFailure",
            blast_radius=BlastRadius.LOW,
            rollback="re-enable",
            action_type=FixActionType.SET_FLAG,
            flag="paymentFailure",
        )
    ]
    trace: list[str] = []
    out = _ensure_executable_action(steps, service="paymentservice", decision_trace=trace)
    assert out[0].flag == "paymentFailure"
    assert trace == []  # already correct — no correction logged


def test_ensure_executable_action_noop_when_unmapped_service():
    steps = [
        RankedFixStep(
            description="page a human",
            blast_radius=BlastRadius.LOW,
            rollback="n/a",
        )
    ]
    out = _ensure_executable_action(steps, service="totally-unknown", decision_trace=[])
    assert out[0].action_type is FixActionType.MANUAL


# ─── executor dispatch (no cluster needed) ─────────────────────────────────


def test_executor_set_flag_requires_a_flag():
    res = execute_rca_fix_step(action="set_flag", flag="")
    assert res.ok is False
    assert "requires a 'flag'" in (res.error or "")


def test_executor_rollback_deploy_is_unsupported_with_clear_message():
    res = execute_rca_fix_step(action="rollback_deploy")
    assert res.ok is False
    assert (res.metadata or {}).get("unsupported_action") == "rollback_deploy"
    assert "manually" in (res.error or "").lower()


def test_executor_manual_action_is_unsupported():
    res = execute_rca_fix_step(action="manual")
    assert res.ok is False
    assert (res.metadata or {}).get("unsupported_action") == "manual"
