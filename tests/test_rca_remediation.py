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
    """The deterministic verdict for user_service_mysql_down must carry an
    executable set_flag action so the executor can follow it directly.

    Tests ``_fallback_verdict`` directly rather than ``analyze`` — the latter
    calls the live LLM when credentials are present, which is non-deterministic
    and would make this assertion flaky. The deterministic verdict is the
    contract we control."""
    verdict = _fallback_verdict(
        {"affected_service": "user-service", "severity": "Sev-1"},
        scenario_id="user_service_mysql_down",
        decision_trace=[],
    )
    primary = verdict.ranked_fix_steps[0]
    assert primary.action_type is FixActionType.SET_FLAG
    assert primary.flag == "user_service.mysql_down"
    assert primary.variant == "off"
    # The second step is diagnostic (check the Secret's credentials), so it is
    # MANUAL. It used to be ROLLBACK_DEPLOY ("helm rollback otel-demo"), which
    # named a release that no longer exists.
    assert verdict.ranked_fix_steps[1].action_type is FixActionType.MANUAL


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
        {"action_type": "set_flag", "flag": "payment_service.redis_down", "variant": "off"}
    )
    assert (action_type, flag, variant) == (
        FixActionType.SET_FLAG,
        "payment_service.redis_down",
        "off",
    )


def test_coerce_action_downgrades_set_flag_without_flag_to_manual():
    action_type, flag, _ = _coerce_action({"action_type": "set_flag"})
    assert action_type is FixActionType.MANUAL
    assert flag is None


def test_coerce_action_unknown_type_becomes_manual():
    action_type, flag, _ = _coerce_action({"action_type": "rm -rf /", "flag": "x"})
    assert action_type is FixActionType.MANUAL
    # flag is dropped for non-set_flag actions.
    assert flag is None


def test_ensure_executable_action_does_not_guess_a_fault_from_the_service_name():
    """A manual step must STAY manual when the service has several faults.

    The old behaviour annotated set_flag using a one-flag-per-service map, so a
    manual step gained an apply button. That map is gone: order-service alone
    has four possible faults (postgres_down, http_500, memory_leak_oom,
    payment_timeout) and the service name cannot discriminate between them.

    Guessing would hand the operator a one-click "fix" for a fault that may not
    be the one occurring — worse than no button, because it looks authoritative.
    Identifying which fault is live is the job of evidence
    (agents/rca_agent/evidence.py), not of a name lookup.
    """
    steps = [
        RankedFixStep(
            description="restart the pod",
            blast_radius=BlastRadius.LOW,
            rollback="n/a",
        )
    ]
    trace: list[str] = []
    out = _ensure_executable_action(steps, service="order-service", decision_trace=trace)
    assert out[0].action_type is FixActionType.MANUAL
    assert out[0].flag is None


def test_ensure_executable_action_keeps_a_real_failure_key():
    """A step naming a REAL failure key keeps its executable annotation.

    Replaces a test that asserted the curated map could correct a hallucinated
    flag ('recommendationFailure' -> 'recommendationCacheFailure'). With several
    faults per service there is nothing to correct *to*; invented keys are
    instead caught downstream by _ensure_executable_action, which checks
    them against the live automation.fault.clear registry and downgrades
    anything unrecognised to manual.
    """
    steps = [
        RankedFixStep(
            description="Clear the order_service.http_500 fault",
            blast_radius=BlastRadius.LOW,
            rollback="re-inject",
            action_type=FixActionType.SET_FLAG,
            flag="order_service.http_500",
        )
    ]
    out = _ensure_executable_action(steps, service="order-service", decision_trace=[])
    assert out[0].action_type is FixActionType.SET_FLAG
    assert out[0].flag == "order_service.http_500"


def test_ensure_executable_action_leaves_correct_flag_untouched():
    steps = [
        RankedFixStep(
            description="Disable paymentFailure",
            blast_radius=BlastRadius.LOW,
            rollback="re-enable",
            action_type=FixActionType.SET_FLAG,
            flag="payment_service.redis_down",
        )
    ]
    trace: list[str] = []
    out = _ensure_executable_action(steps, service="paymentservice", decision_trace=trace)
    assert out[0].flag == "payment_service.redis_down"
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


def test_ensure_executable_action_downgrades_invented_flag_for_unmapped_service(monkeypatch):
    """Regression: the LLM invents 'frontendFailure' (no such flag) for the
    unmapped 'frontend' service. Grounding against the live flagd config must
    downgrade it to manual so the UI never offers an un-runnable apply."""
    import agents.rca_agent.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "_live_flag_names",
        lambda: {"payment_service.redis_down", "order_service.http_500", "adFailure"},
    )
    steps = [
        RankedFixStep(
            description="Set flag frontendFailure off",
            blast_radius=BlastRadius.LOW,
            rollback="re-flip",
            action_type=FixActionType.SET_FLAG,
            flag="frontendFailure",
            variant="off",
        )
    ]
    trace: list[str] = []
    out = _ensure_executable_action(steps, service="frontend", decision_trace=trace)
    assert out[0].action_type is FixActionType.MANUAL
    assert out[0].flag is None
    # Wording updated in Phase 5: the message no longer says "flagd", which was removed
    # from this repo two migrations before the check was rewritten. The behaviour asserted
    # above is unchanged, and this run caught a real regression in it — service-scoping had
    # made "the registry has no action for this service" (authoritative) look like "nobody
    # could tell us what is runnable" (ignorance), so the invented key survived.
    assert any("downgraded fix step" in line for line in trace)
    assert any("no executable action for 'frontend'" in line for line in trace)


def test_ensure_executable_action_keeps_real_flag_for_unmapped_service(monkeypatch):
    """A real flag for a service the curated map doesn't cover is kept as-is."""
    import agents.rca_agent.agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "_live_flag_names", lambda: {"emailMemoryLeak", "payment_service.redis_down"}
    )
    steps = [
        RankedFixStep(
            description="Set flag emailMemoryLeak off",
            blast_radius=BlastRadius.LOW,
            rollback="re-flip",
            action_type=FixActionType.SET_FLAG,
            flag="emailMemoryLeak",
            variant="off",
        )
    ]
    out = _ensure_executable_action(steps, service="emailservice", decision_trace=[])
    assert out[0].action_type is FixActionType.SET_FLAG
    assert out[0].flag == "emailMemoryLeak"


def test_ground_set_flags_skips_lookup_when_no_set_flag_step(monkeypatch):
    """Fail-open + no wasted flagd call: a manual-only step list is unchanged
    and never triggers the live flag lookup."""
    import agents.rca_agent.agent as agent_mod

    called = {"n": 0}

    def _boom() -> set[str]:
        called["n"] += 1
        raise AssertionError("should not fetch flags when there's no set_flag step")

    monkeypatch.setattr(agent_mod, "_live_flag_names", _boom)
    steps = [RankedFixStep(description="investigate", blast_radius=BlastRadius.LOW, rollback="n/a")]
    out = _ensure_executable_action(steps, service="totally-unknown", decision_trace=[])
    assert out[0].action_type is FixActionType.MANUAL
    assert called["n"] == 0


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
