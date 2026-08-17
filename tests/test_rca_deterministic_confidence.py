"""The platform owns the confidence number; the model owns the prose.

Requirement: *"The LLM's confidence value must NOT determine the final confidence
score."* Phase 1 could only enforce the zero-evidence case (a cap). Phase 2 has the
evidence matrix, so the number is now computed from classified evidence and the model's
figure is recorded for calibration and otherwise ignored.

These tests drive ``analyze`` with a fake LLM so the model's answer is a controlled input
rather than a live variable. That is the only way to assert "the model said 0.99 and the
verdict says 0.82" — which is the whole property.
"""

from __future__ import annotations

import json

import pytest

from agents.rca_agent import agent as rca
from agents.rca_agent import evidence as _evidence
from agents.rca_agent.investigation.models import RootCauseStatus
from aiops.context.models import SectionStatus
from aiops.context.pack import (
    ContextSection,
    IncidentContext,
    IncidentIdentity,
    SecurityMetadata,
    SourceProvenance,
)

TRIAGE = {
    "affected_service": "order-service",
    "severity": "Sev-1",
    "alert_summary": "EcommercePostgresDown firing: postgres_connection_status at 0.0",
    "audit_metadata": {
        "created_at": "2026-08-03T10:00:00Z",
        "source_alerts": ["ALT-order-service-postgres-down"],
    },
}

# A decisive store outage: one gauge down, two healthy, plus a corroborating log line so
# the matrix earns cross-source agreement — the shape that produces a CONFIRMED verdict.
_METRICS_RAW = {
    "postgres_connection_status": {"results": [{"metric": {}, "value": [0, "0"]}]},
    "mysql_connection_status": {"results": [{"metric": {}, "value": [0, "1"]}]},
    "redis_connection_status": {"results": [{"metric": {}, "value": [0, "1"]}]},
    _evidence.ALERTS_QUERY_ID: {
        "alerts": [{"state": "firing", "labels": {"alertname": "EcommercePostgresDown"}}]
    },
}
_LOGS_RAW = {
    _evidence.LOGS_QUERY_ID: {
        "streams": [
            {
                "stream": {"level": "ERROR"},
                "values": [["1754222400000000000", "ERROR database connection failed"]],
            }
        ]
    }
}


def _section(status: SectionStatus, raw: dict | None) -> ContextSection:
    return ContextSection(
        status=status,
        provenance=SourceProvenance(provider="test", status=status),
        raw=raw if status.usable else None,
    )


def _skipped() -> ContextSection:
    return ContextSection(
        status=SectionStatus.NOT_REQUESTED,
        provenance=SourceProvenance(provider="test", status=SectionStatus.NOT_REQUESTED),
    )


def _context(*, metrics: SectionStatus = SectionStatus.COLLECTED) -> dict:
    from datetime import UTC, datetime

    end = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    pack = IncidentContext(
        incident=IncidentIdentity(
            service="order-service",
            severity="critical",
            window_start=end,
            window_end=end,
            correlation_id="test-corr",
            alert_name="EcommercePostgresDown",
        ),
        built_at=end,
        metrics=_section(metrics, _METRICS_RAW),
        logs=_section(SectionStatus.COLLECTED, _LOGS_RAW),
        traces=_skipped(),
        k8s_events=_skipped(),
        topology=_skipped(),
        dependencies=_skipped(),
        deployments=_skipped(),
        incident_history=_skipped(),
        oncall=_skipped(),
        cmdb=_skipped(),
        runbooks=_skipped(),
        security=SecurityMetadata(redaction_applied=False),
    )
    return pack.model_dump(mode="json")


@pytest.fixture(autouse=True)
def _context_on(monkeypatch):
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    # Change correlation would reach the SCM seam; neutralised so these tests assert the
    # agent rather than whether a GitHub token happens to be configured.
    monkeypatch.setattr(rca, "_fetch_change_evidence", lambda *a, **k: None)


def _fake_llm(monkeypatch, *, root_cause: str, confidence: float):
    """Install a fake LLM returning a controlled verdict."""

    class _Resp:
        text = json.dumps(
            {
                "root_cause": root_cause,
                "confidence_score": confidence,
                "ranked_fix_steps": [
                    {
                        "description": "Restore the datastore.",
                        "blast_radius": "low",
                        "rollback": "Scale back down.",
                        "action_type": "manual",
                    }
                ],
            }
        )

    monkeypatch.setattr(rca, "llm_complete", lambda **_kwargs: _Resp())


# ─── the model does not own the number ──────────────────────────────────────


def test_model_confidence_is_recorded_but_not_used(monkeypatch):
    """The headline requirement.

    The model claims near-certainty; the verdict reports the deterministic score of the
    hypothesis the evidence actually supports, and keeps the model's figure separately so
    the gap between the two is measurable.
    """
    _fake_llm(
        monkeypatch,
        root_cause="PostgreSQL is unreachable from order-service.",
        confidence=0.99,
    )
    verdict = rca.analyze(TRIAGE, context=_context())

    assert verdict.llm_stated_confidence == pytest.approx(0.99)
    assert verdict.confidence_score != pytest.approx(0.99)
    assert verdict.investigation is not None
    assert verdict.confidence_score == pytest.approx(verdict.investigation.confidence)
    assert any("recorded, not used" in line for line in verdict.audit_metadata.decision_trace)


def test_a_pessimistic_model_does_not_lower_the_score_either(monkeypatch):
    """The override is symmetric — it is not a cap dressed up as a rule.

    A model that under-claims must not drag down a well-supported conclusion, or the
    "platform owns the number" property would only hold in one direction.
    """
    _fake_llm(
        monkeypatch, root_cause="PostgreSQL is unreachable from order-service.", confidence=0.05
    )
    verdict = rca.analyze(TRIAGE, context=_context())
    assert verdict.confidence_score > 0.05
    assert verdict.root_cause_status is RootCauseStatus.CONFIRMED


def test_prose_that_ignores_the_ranked_hypothesis_is_downgraded(monkeypatch):
    """A number computed for one claim must not be presented beside a different one.

    If the model writes about something the platform did not rank, the pair is not
    corroborated — and a confident-looking number beside unrelated prose is worse than
    either being wrong alone, because it reads as agreement.
    """
    _fake_llm(
        monkeypatch,
        root_cause="A cosmic ray flipped a bit in the load balancer.",
        confidence=0.95,
    )
    verdict = rca.analyze(TRIAGE, context=_context())
    assert verdict.root_cause_status is RootCauseStatus.UNCERTAIN
    assert verdict.confidence_score <= 0.5
    assert any("does not describe" in line for line in verdict.audit_metadata.decision_trace)


def test_investigation_is_attached_to_the_verdict(monkeypatch):
    """The structured result travels with the verdict, so it can be rendered and audited."""
    _fake_llm(monkeypatch, root_cause="PostgreSQL is unreachable.", confidence=0.8)
    verdict = rca.analyze(TRIAGE, context=_context())
    investigation = verdict.investigation

    assert investigation is not None
    assert investigation.scope.affected_service == "order-service"
    assert investigation.matrices, "hypotheses should have been ranked"
    assert investigation.selected is not None
    assert investigation.selected.hypothesis.category == "dependency_unavailable"
    assert investigation.completeness.overall > 0
    # "Why the others lost" is half a reviewable RCA.
    assert isinstance(investigation.rejected, tuple)


def test_verdict_with_investigation_round_trips_through_json(monkeypatch):
    """It is persisted and crosses the HTTP boundary, so the nested model must survive."""
    from agents.rca_agent.models import RCAVerdict

    _fake_llm(monkeypatch, root_cause="PostgreSQL is unreachable.", confidence=0.8)
    verdict = rca.analyze(TRIAGE, context=_context())
    restored = RCAVerdict.model_validate(json.loads(json.dumps(verdict.model_dump(mode="json"))))

    assert restored.investigation is not None
    assert restored.confidence_score == pytest.approx(verdict.confidence_score)
    assert restored.investigation.selected.hypothesis.category == "dependency_unavailable"


# ─── no LLM required ────────────────────────────────────────────────────────


def test_a_verdict_is_produced_with_no_llm_at_all(monkeypatch):
    """The deterministic path stands on its own.

    Before Phase 2 an unavailable model meant "insufficient evidence" however decisive the
    telemetry was, because the model was the only thing that could name a cause. Now the
    cause is chosen by scored rules, so the stub provider yields a real verdict.
    """
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    monkeypatch.delenv("AIOPS_RCA_LLM_PROVIDER", raising=False)

    verdict = rca.analyze(TRIAGE, context=_context())
    assert verdict.root_cause_status is RootCauseStatus.CONFIRMED
    assert "unreachable" in verdict.root_cause.lower()
    assert verdict.llm_stated_confidence is None, "no model spoke, so there is no figure to record"
    assert any("without an LLM" in line for line in verdict.audit_metadata.decision_trace)


def test_the_no_llm_verdict_proposes_no_executable_action(monkeypatch):
    """Grounding decides what is runnable, and it has not run yet (Phase 5).

    A hypothesis carries a generic remediation *class*; turning that into a key here would
    be the agent inventing an executable action, which is what action grounding exists to
    prevent.
    """
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    verdict = rca.analyze(TRIAGE, context=_context())
    assert all(step.action_type.value == "manual" for step in verdict.ranked_fix_steps)
    assert all(step.flag is None for step in verdict.ranked_fix_steps)
    assert all(step.requires_hitl is True for step in verdict.ranked_fix_steps)


# ─── context-layer modes ────────────────────────────────────────────────────


def test_unavailable_metrics_section_yields_no_confident_cause(monkeypatch):
    """A blind spot must not become a conclusion.

    With the metrics section unusable, the rules see gaps rather than absence, so nothing
    can be corroborated and the investigation cannot settle on a cause.
    """
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    verdict = rca.analyze(TRIAGE, context=_context(metrics=SectionStatus.UNAVAILABLE))
    assert not verdict.root_cause_status.is_actionable


def test_offline_runs_no_investigation_and_still_abstains(monkeypatch):
    """``run()``'s path: no retrieval, so no facts, so no investigation."""
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    verdict = rca.analyze(TRIAGE, offline=True)
    assert verdict.investigation is None
    assert verdict.root_cause_status is RootCauseStatus.INSUFFICIENT_EVIDENCE
    assert verdict.confidence_score <= rca.NO_EVIDENCE_CONFIDENCE_CEILING


def test_context_off_preserves_the_legacy_path(monkeypatch):
    """``AIOPS_CONTEXT_LAYER=off`` must not consume the Context Pack.

    The rollout gate has to keep working in all three modes; ``off`` is the default and
    the CI path, and a context passed while off must be ignored rather than half-used.
    """
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "off")
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    monkeypatch.setattr(rca._evidence, "gather", lambda *a, **k: {})

    verdict = rca.analyze(TRIAGE, context=_context())
    assert verdict.root_cause_status is RootCauseStatus.INSUFFICIENT_EVIDENCE
    assert not any(
        "from the shared context" in line for line in verdict.audit_metadata.decision_trace
    )


def test_shadow_mode_does_not_change_the_verdict(monkeypatch):
    """Shadow builds the context for comparison only; the legacy answer stays authoritative.

    Including the *facts*: reading the prompt from one source and the score from another
    would make shadow mode change the verdict, which is the one thing it must never do.
    """
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    monkeypatch.setattr(rca._evidence, "gather", lambda *a, **k: {})

    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "off")
    off = rca.analyze(TRIAGE, context=_context())
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "shadow")
    shadow = rca.analyze(TRIAGE, context=_context())

    assert shadow.root_cause_status is off.root_cause_status
    assert shadow.confidence_score == pytest.approx(off.confidence_score)


def test_a_failing_investigation_does_not_lose_the_verdict(monkeypatch):
    """A bug in the stages costs the structured result, never the RCA.

    Same posture as every other lookup in this agent: a defect degrades the answer rather
    than failing the incident.
    """
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")

    def explode(*_a, **_k):
        raise RuntimeError("stages are broken")

    monkeypatch.setattr(
        "agents.rca_agent.investigation.pipeline.investigate", explode, raising=True
    )
    verdict = rca.analyze(TRIAGE, context=_context())
    assert verdict.investigation is None
    assert verdict.root_cause  # a verdict was still produced
    assert any("raised RuntimeError" in line for line in verdict.audit_metadata.decision_trace)
