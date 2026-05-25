"""Tests for the Alert Triage severity classifier.

Covers the tolerant LLM-response parser and the decision-trace dispatch
that distinguishes a real LLM verdict from the parse-failure fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

# ─── pure-parser tests ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected_sev,expected_conf",
    [
        # Canonical form from the system prompt.
        ("Severity: Sev-1\nConfidence: 0.95", "Sev-1", 0.95),
        # Case drift.
        ("severity: sev-2\nconfidence: 0.8", "Sev-2", 0.8),
        ("SEVERITY: SEV-3\nCONFIDENCE: 0.7", "Sev-3", 0.7),
        # Separator drift (no dash, space, colon).
        ("Sev 1\nConfidence 0.9", "Sev-1", 0.9),
        ("Sev1, confidence=0.5", "Sev-1", 0.5),
        ("Sev:2 confidence 0.75", "Sev-2", 0.75),
        # Verbose label.
        ("Severity 1 with confidence 0.88", "Sev-1", 0.88),
        ("Severity-4, confidence 0.3", "Sev-4", 0.3),
        ("severity 2 confidence: 0.65", "Sev-2", 0.65),
        # Confidence omitted -> default 0.6.
        ("Sev-3", "Sev-3", 0.6),
        ("Severity: Sev-2 — customer impact unclear.", "Sev-2", 0.6),
        # Confidence out of range gets clamped.
        ("Sev-1 confidence 1.5", "Sev-1", 1.0),
        ("Sev-4 confidence -0.2", "Sev-4", 0.0),
    ],
)
def test_parse_severity_response_accepts_variants(
    text: str, expected_sev: str, expected_conf: float
) -> None:
    from agents.alert_triage.agent import _parse_severity_response

    result = _parse_severity_response(text)
    assert result is not None, f"parser rejected: {text!r}"
    sev, conf = result
    assert sev == expected_sev
    assert conf == pytest.approx(expected_conf)


@pytest.mark.parametrize(
    "text",
    [
        # Empty / whitespace.
        "",
        "   \n",
        # No severity verdict.
        "I am not able to classify this alert.",
        "confidence: 0.9",  # confidence alone is not enough
        # Out-of-range Sev-N — model hallucinated a non-existent level.
        "Sev-7 confidence 0.9",
        "Severity 5",
        # Embedded in a word — must not match.
        "page 1 of the runbook",
        "no severityyy info",  # extra letters after 'severity'
    ],
)
def test_parse_severity_response_rejects_garbage(text: str) -> None:
    from agents.alert_triage.agent import _parse_severity_response

    assert _parse_severity_response(text) is None, f"parser should have rejected: {text!r}"


# ─── integration: trace-line dispatch ──────────────────────────────────────


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    """Same fixture shape as the dedup tests: isolated SQLite + stub provider."""
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    db_path = tmp_path / "test_state.db"
    monkeypatch.setenv("AIOPS_STATE_DB_URL", f"sqlite:///{db_path.as_posix()}")

    from aiops.state import init_db, reset_engine_for_tests

    reset_engine_for_tests()
    init_db()

    from agents.alert_triage.agent import reset_dedup_store

    reset_dedup_store()

    yield

    reset_engine_for_tests()


def _ambiguous_alert_input() -> dict[str, Any]:
    """Alert whose rule-based classifier returns (None, 0.5) so the LLM
    consult path is exercised: no severity_hint, no threshold, non-CPU/mem
    metric, and a non-customer-facing service."""
    return {
        "alert_id": "ALT-SEV-TEST",
        "service": "internal-batch",
        "metric": "queue_depth",
        "value": 42.0,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "Prometheus",
        "labels": {},
        "annotations": {},
    }


def _install_fake_llm(monkeypatch, response_text: str) -> None:
    """Patch the agent module's ``llm_complete`` symbol with a stub returning
    ``response_text``. The agent imports it at module load, so patching the
    module attribute is what matters."""
    from agents.alert_triage import agent as agent_mod

    class _Resp:
        text = response_text

    def _fake_complete(*args, **kwargs):
        return _Resp()

    monkeypatch.setattr(agent_mod, "llm_complete", _fake_complete)


def test_llm_success_emits_inferred_trace_line(clean_state, monkeypatch):
    """Parseable LLM response → trace line says 'inferred from LLM consult'."""
    from agents.alert_triage import run

    _install_fake_llm(monkeypatch, "Severity: Sev-2\nConfidence: 0.85")

    v = run(_ambiguous_alert_input())

    assert v["severity"] == "Sev-2"
    assert v["confidence_score"] == pytest.approx(0.85)
    trace = v["audit_metadata"]["decision_trace"]
    assert any("inferred from LLM consult" in line for line in trace), trace
    assert not any("failed to return a parseable verdict" in line for line in trace), trace


def test_llm_parse_failure_emits_distinct_trace_line(clean_state, monkeypatch):
    """Unparseable LLM response → trace line must say the consult FAILED and
    the verdict is the deterministic fallback. Regression for the silent
    Sev-3 / 0.4 fallback that masqueraded as a real LLM verdict."""
    from agents.alert_triage import run

    _install_fake_llm(monkeypatch, "I cannot help with that.")

    v = run(_ambiguous_alert_input())

    assert v["severity"] == "Sev-3"
    assert v["confidence_score"] == pytest.approx(0.4)
    trace = v["audit_metadata"]["decision_trace"]
    assert any("LLM consult failed" in line for line in trace), trace
    # Must NOT also claim it was inferred from the LLM.
    assert not any("inferred from LLM consult" in line for line in trace), trace


def test_llm_exception_also_emits_distinct_trace_line(clean_state, monkeypatch):
    """Provider exception path: same distinguishable trace line as parse
    failure. The user shouldn't have to guess which branch fired."""
    from agents.alert_triage import agent as agent_mod
    from agents.alert_triage import run

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(agent_mod, "llm_complete", _boom)

    v = run(_ambiguous_alert_input())

    assert v["severity"] == "Sev-3"
    assert v["confidence_score"] == pytest.approx(0.4)
    trace = v["audit_metadata"]["decision_trace"]
    assert any("LLM consult failed" in line for line in trace), trace


# ─── DEMO-SEV-ROUTING (#131): ScenarioActive synthetic-alert override ──────


def _scenario_active_alert(flag: str, **overrides: Any) -> dict[str, Any]:
    """Mirror what ``demo/ui/server.py`` emits when a scenario flag is on:
    a Prometheus ``ScenarioActive`` alert with ``alert_type=scenario_active``
    and ``flag=<flagName>`` in labels."""
    base: dict[str, Any] = {
        "alert_id": f"PROM-ScenarioActive-{flag}",
        "service": "payment",
        "metric": "ScenarioActive",
        "value": 1.0,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "Prometheus",
        "labels": {
            "alert_type": "scenario_active",
            "flag": flag,
            "alertname": "ScenarioActive",
            "severity": "high",
        },
        "annotations": {"summary": f"Scenario {flag} active"},
    }
    base.update(overrides)
    return base


def test_scenario_active_payment_failure_forces_sev1(clean_state):
    """Demo-critical scenario flag → Sev-1 + high confidence so the router
    routes to ``incidents`` channel with ``page_oncall`` action. Without
    this override the LLM sees the synthetic alert, finds no error-rate
    evidence in metrics, and returns Sev-4 — breaking the demo's
    'phone-buzz' beat at T+0:20."""
    from agents.alert_triage import run

    v = run(_scenario_active_alert("paymentFailure"))

    assert v["severity"] == "Sev-1", v["audit_metadata"]["decision_trace"]
    assert v["confidence_score"] >= 0.9


def test_scenario_active_unknown_flag_falls_through_to_default_logic(
    clean_state, monkeypatch
):
    """An unmapped flag (not in the demo-critical list) must NOT take the
    override path — it should drop through to the normal classifier so
    we don't accidentally page on every synthetic alert."""
    from agents.alert_triage import run

    # Use an obviously-not-demo flag name so the override doesn't fire.
    # The alert has severity_hint="high" via the labels.severity, but no
    # threshold and a non-CPU/mem metric — the rule-based classifier will
    # still pick up the s_hint and return Sev-2.
    v = run(_scenario_active_alert("randomFlag"))

    # Must NOT be Sev-1 (the override didn't fire).
    assert v["severity"] != "Sev-1", v["audit_metadata"]["decision_trace"]


def test_non_scenario_active_alert_unaffected_by_override(clean_state):
    """A regular Prometheus alert (not the ScenarioActive synthetic) must
    NOT take the override path even if its flag label happens to match a
    demo-critical name. The override is keyed strictly on
    ``alert_type == scenario_active``."""
    from agents.alert_triage import run

    real_payment_alert = {
        "alert_id": "PROM-PaymentErrorRateHigh-1",
        "service": "payment",
        "metric": "PaymentErrorRateHigh",
        "value": 0.15,
        "threshold": 0.05,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "Prometheus",
        "labels": {
            "alertname": "PaymentErrorRateHigh",
            "flag": "paymentFailure",  # same flag name but different alert_type
            "severity": "high",
        },
        "annotations": {},
    }
    v = run(real_payment_alert)

    # Real PaymentErrorRateHigh with ratio=3.0 on customer-facing service
    # → Sev-1 from the ratio rule, NOT from the override (which requires
    # alert_type=scenario_active).
    assert v["severity"] in {"Sev-1", "Sev-2"}, (
        f"got {v['severity']}; decision_trace={v['audit_metadata']['decision_trace']}"
    )


def test_scenario_active_override_works_via_pure_function():
    """Direct unit test of the rule-based classifier so the override is
    locked-in regardless of integration paths through ``run()``."""
    from agents.alert_triage.agent import _classify_severity_rule_based
    from agents.alert_triage.models import Alert

    alert = Alert(
        alert_id="PROM-ScenarioActive-1",
        service="payment",
        metric="ScenarioActive",
        value=1.0,
        timestamp=datetime.now(UTC),
        source="Prometheus",
        labels={
            "alert_type": "scenario_active",
            "flag": "paymentFailure",
        },
        annotations={},
    )

    sev, conf = _classify_severity_rule_based(alert)
    assert sev == "Sev-1"
    assert conf >= 0.9


def test_scenario_active_override_does_not_fire_without_flag_label():
    """If somehow a scenario_active alert arrives WITHOUT a flag label,
    don't crash — fall through to the normal logic."""
    from agents.alert_triage.agent import _classify_severity_rule_based
    from agents.alert_triage.models import Alert

    alert = Alert(
        alert_id="PROM-ScenarioActive-noflag",
        service="payment",
        metric="ScenarioActive",
        value=1.0,
        timestamp=datetime.now(UTC),
        source="Prometheus",
        labels={"alert_type": "scenario_active"},  # no flag
        annotations={},
    )
    sev, conf = _classify_severity_rule_based(alert)
    # Falls through; either rule-based fires (None) or returns something
    # other than the forced Sev-1.
    assert sev != "Sev-1" or conf != 0.95
