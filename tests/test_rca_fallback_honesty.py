"""The RCA must abstain rather than guess, and must never guess from a service name.

Two defects motivated this file.

**The service-name fallback.** ``_fallback_verdict`` used to match
``scenario_id == "user_service_mysql_down" or service in _LOCKED_SERVICES``, where
that frozenset contained ``"user-service"``. So *any* user-service incident with no
usable LLM returned a hand-written MySQL root cause at confidence 0.85 — for a
service with four distinct failure modes. The alert could be a crashloop, a latency
regression or CPU saturation and the answer was always MySQL.

**Unbounded confidence on no evidence.** The model's ``confidence_score`` was passed
through verbatim, so a run that observed nothing could still return 0.9.

Both are asserted here rather than only in the eval matrix, because they are safety
properties: an operator is offered a button based on this output, and a test that
only runs when someone remembers to run the accuracy matrix is not a guard.

Everything is hermetic. ``tests/conftest.py`` sets ``AIOPS_LLM_PROVIDER=stub``
autouse, and ``agent._rca_provider`` now defers to an explicit platform stub, so
``analyze`` reaches no network. That deferral is itself new — before it, this agent
asked Anthropic even under the stub and landed in the exception path instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.rca_agent import agent as rca
from agents.rca_agent.investigation.models import RootCauseStatus
from evals.rca_truth import discover_ecommerce_truth_files, load_truth, rca_input_from_truth

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_retrieval(monkeypatch):
    """Neutralise every retrieval path so these tests assert the agent, not the world.

    ``_evidence.gather`` and ``_fetch_change_evidence`` both reach the tool registry,
    which — with a ``.env`` carrying a Prometheus URL or a GitHub token — would make
    these results depend on whether a cluster happens to be port-forwarded. The point
    here is the zero-evidence contract, so evidence is removed deliberately rather
    than left to chance.
    """
    monkeypatch.setattr(rca._evidence, "gather", lambda *a, **k: {})
    monkeypatch.setattr(rca, "_fetch_change_evidence", lambda *a, **k: None)


def _verdict_for(service: str, summary: str = "something is wrong"):
    return rca.analyze(
        {
            "affected_service": service,
            "severity": "Sev-1",
            "alert_summary": summary,
            "audit_metadata": {"decision_trace": ["received alert"]},
        }
    )


# ─── the service-name fallback is gone ──────────────────────────────────────


def test_user_service_incident_does_not_become_mysql_down():
    """The regression this file exists for.

    A user-service incident with no scenario hint and no evidence must not be told it
    is a MySQL outage. ``user-service`` has four failure modes and a service name
    cannot distinguish them.
    """
    verdict = _verdict_for("user-service", "EcommerceUserServiceCPUHigh firing: cpu at 0.95")
    assert "mysql" not in verdict.root_cause.lower()
    assert verdict.root_cause_status is RootCauseStatus.INSUFFICIENT_EVIDENCE
    assert verdict.confidence_score <= rca.NO_EVIDENCE_CONFIDENCE_CEILING


@pytest.mark.parametrize("service", ["user-service", "userservice", "user", "USER-SERVICE"])
def test_no_spelling_of_user_service_triggers_the_locked_verdict(service):
    """Every spelling the deleted frozenset held, including the case-folded one."""
    verdict = _verdict_for(service)
    assert "mysql statefulset" not in verdict.root_cause.lower()
    assert verdict.confidence_score < 0.5


def test_missing_service_does_not_produce_a_confident_cause():
    """A verdict with no ``affected_service`` must not name one.

    This previously defaulted to ``productcatalogservice``, which was in the locked
    set — so a malformed input produced a confident root cause about a service that
    was never involved and no longer exists.
    """
    verdict = rca.analyze({"severity": "Sev-2", "alert_summary": "?"})
    assert verdict.affected_service == "unknown"
    assert verdict.root_cause_status is RootCauseStatus.INSUFFICIENT_EVIDENCE


def test_explicit_scenario_id_still_reaches_the_locked_verdict():
    """The demo path is preserved — only the service-name branch was removed.

    ``demo/failure_injection`` passes ``scenario_id`` explicitly, and that rehearsed
    flow keeps working. Asserted so the fix is visibly a narrowing rather than a
    removal. (The evaluation deliberately never passes this hint — see
    ``tests/test_rca_eval_blindness.py``.)
    """
    verdict = rca.analyze(
        {"affected_service": "user-service", "severity": "Sev-1", "alert_summary": "500s"},
        scenario_id="user_service_mysql_down",
    )
    assert "mysql" in verdict.root_cause.lower()
    assert verdict.confidence_score == pytest.approx(0.85)


# ─── confidence is bounded by evidence ──────────────────────────────────────


def test_confidence_is_capped_when_nothing_was_observed():
    trace: list[str] = []
    assert rca._bounded_confidence(0.95, has_evidence=False, decision_trace=trace) == pytest.approx(
        rca.NO_EVIDENCE_CONFIDENCE_CEILING
    )
    assert any("capped confidence" in line for line in trace), "the cap must be auditable"


def test_confidence_is_untouched_when_evidence_exists():
    """The cap is narrow on purpose: it fires only on the provably unsupported case."""
    trace: list[str] = []
    assert rca._bounded_confidence(0.95, has_evidence=True, decision_trace=trace) == pytest.approx(
        0.95
    )
    assert trace == []


def test_low_confidence_is_not_raised_by_the_cap():
    """A ceiling, never a floor."""
    trace: list[str] = []
    assert rca._bounded_confidence(0.05, has_evidence=False, decision_trace=trace) == pytest.approx(
        0.05
    )


def test_status_ignores_confidence_when_no_evidence_was_seen():
    """With nothing observed there is no conclusion to grade.

    Short-circuits before any threshold, because "the signals did not discriminate"
    and "we could not look" are different answers.
    """
    assert rca._derive_status(0.99, has_evidence=False) is RootCauseStatus.INSUFFICIENT_EVIDENCE


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (0.95, RootCauseStatus.CONFIRMED),
        (0.75, RootCauseStatus.CONFIRMED),
        (0.60, RootCauseStatus.PROBABLE),
        (0.50, RootCauseStatus.PROBABLE),
        (0.40, RootCauseStatus.UNCERTAIN),
        (0.30, RootCauseStatus.UNCERTAIN),
        (0.20, RootCauseStatus.INSUFFICIENT_EVIDENCE),
    ],
)
def test_status_thresholds_track_the_prompt_wording(confidence, expected):
    """Thresholds mirror the system prompt's own guidance, so the two cannot drift."""
    assert rca._derive_status(confidence, has_evidence=True) is expected


# ─── the whole eval set abstains without evidence ───────────────────────────


@pytest.mark.parametrize("path", discover_ecommerce_truth_files(), ids=lambda p: p.stem)
def test_every_scenario_abstains_with_no_evidence_and_no_llm(path):
    """The sharp contract, over all 12 scenarios.

    Given only a blind triage verdict — no telemetry, no LLM — the honest answer is
    always "insufficient evidence". A confident cause here would be pure confabulation
    from an alert name, and this is the assertion that makes the CI signal meaningful
    despite the golden file only being able to check evidence-independent properties.
    """
    payload = rca_input_from_truth(load_truth(path))
    verdict = rca.analyze(payload["triage_verdict"])

    assert verdict.root_cause_status is RootCauseStatus.INSUFFICIENT_EVIDENCE
    assert verdict.confidence_score <= rca.NO_EVIDENCE_CONFIDENCE_CEILING
    assert not verdict.root_cause_status.is_actionable


@pytest.mark.parametrize("path", discover_ecommerce_truth_files(), ids=lambda p: p.stem)
def test_every_fix_step_stays_hitl_gated_and_runnable(path):
    """Safety invariants that hold on every path, including the fallbacks.

    ``requires_hitl`` is typed ``Literal[True]`` so it cannot be false; asserted
    anyway because this is the property the platform gate depends on. The second
    assertion catches a ``set_flag`` step with no key — a button that fails *after*
    the human has already approved it.
    """
    payload = rca_input_from_truth(load_truth(path))
    verdict = rca.analyze(payload["triage_verdict"])

    assert verdict.ranked_fix_steps, "a verdict must always propose at least one step"
    for i, step in enumerate(verdict.ranked_fix_steps, start=1):
        assert step.requires_hitl is True, f"step #{i} is not HITL-gated"
        if step.action_type.value == "set_flag":
            assert step.flag, f"step #{i} is set_flag with no action key"


def test_abstaining_verdict_proposes_only_manual_action():
    """An abstention must not ship an executable button.

    Offering a one-click fix beside "insufficient evidence" invites the operator to
    act on a conclusion the agent just said it does not have.
    """
    verdict = _verdict_for("payment-service")
    assert verdict.root_cause_status is RootCauseStatus.INSUFFICIENT_EVIDENCE
    assert all(step.action_type.value == "manual" for step in verdict.ranked_fix_steps)


# ─── provider resolution ────────────────────────────────────────────────────


def test_platform_stub_provider_is_respected(monkeypatch):
    """CI's ``AIOPS_LLM_PROVIDER=stub`` must actually reach this agent.

    Previously the agent pinned "anthropic" regardless, so under the stub it asked a
    real provider, failed on absent credentials, and took the *exception* fallback —
    CI was exercising an error path while appearing to exercise the stub.
    """
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    monkeypatch.delenv("AIOPS_RCA_LLM_PROVIDER", raising=False)
    assert rca._rca_provider() == "stub"


def test_explicit_agent_override_still_wins(monkeypatch):
    """The Azure content-filter workaround stays available."""
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    monkeypatch.setenv("AIOPS_RCA_LLM_PROVIDER", "anthropic")
    assert rca._rca_provider() == "anthropic"


def test_provider_is_read_per_call_not_at_import(monkeypatch):
    """``monkeypatch.setenv`` must move it.

    The value used to be a module constant read at import, which is the bug class
    ``aiops/context/config.py`` documents: a fixture could not change it, so a test
    had to patch a private name instead. ``tests/test_incident_commander.py`` did
    exactly that until this change.
    """
    monkeypatch.setenv("AIOPS_RCA_LLM_PROVIDER", "ollama")
    assert rca._rca_provider() == "ollama"
    monkeypatch.setenv("AIOPS_RCA_LLM_PROVIDER", "openai")
    assert rca._rca_provider() == "openai"


# ─── backward compatibility ─────────────────────────────────────────────────


def test_verdict_keeps_the_locked_v0_wire_fields():
    """The five fields the dashboard and the eval grammar read are unchanged.

    New investigation fields are additive and optional; this asserts none of them
    displaced the locked contract.
    """
    verdict = _verdict_for("order-service")
    dumped = verdict.model_dump(mode="json")
    for field in (
        "affected_service",
        "root_cause",
        "ranked_fix_steps",
        "confidence_score",
        "audit_metadata",
    ):
        assert field in dumped, f"locked v0 field {field!r} missing from the verdict"
    assert dumped["root_cause_status"] == "insufficient_evidence"
    assert "llm_stated_confidence" in dumped


def test_verdict_round_trips_through_json():
    """It is persisted (``RCAResultRow``) and crosses the HTTP boundary, so it must."""
    from agents.rca_agent.models import RCAVerdict

    original = _verdict_for("payment-service")
    restored = RCAVerdict.model_validate(json.loads(json.dumps(original.model_dump(mode="json"))))
    assert restored.root_cause == original.root_cause
    assert restored.root_cause_status is original.root_cause_status


def test_a_stored_pre_phase1_verdict_still_validates():
    """Verdicts already written to ``rca_results`` predate the new fields.

    They must keep loading, or the SNOW watcher and the resolution verifier break on
    historical rows.
    """
    from agents.rca_agent.models import RCAVerdict

    legacy = {
        "affected_service": "order-service",
        "root_cause": "PostgreSQL is scaled to zero.",
        "ranked_fix_steps": [
            {
                "description": "Scale PostgreSQL back to 1.",
                "blast_radius": "low",
                "rollback": "Scale back to 0.",
                "requires_hitl": True,
            }
        ],
        "confidence_score": 0.8,
        "audit_metadata": {"created_at": "2026-08-03T10:00:00Z", "decision_trace": []},
    }
    restored = RCAVerdict.model_validate(legacy)
    assert restored.root_cause_status is RootCauseStatus.UNCERTAIN
    assert restored.llm_stated_confidence is None
