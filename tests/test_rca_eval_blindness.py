"""The RCA agent must never be handed the answer.

This is the test that makes every other RCA accuracy number meaningful. A truth file
contains ``root_cause``, ``failure_key``, ``fault_category``, ``remediation`` and a
``grading`` block, and it sits in the same JSON object as ``expected_alert_payload``
— so "pass the truth file to the agent" is one careless line away, and the resulting
evaluation would score a lookup at ~100% while measuring nothing.

Both directions are asserted, because only checking one is a trap:

* the agent input carries **no** truth content, and
* the harness's grading key **does** carry it.

Without the second assertion a bug that returned an empty grading key would make the
first assertion pass trivially and every scenario score as correct.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.rca_truth import (
    FORBIDDEN_TRUTH_FIELDS,
    TruthLeakError,
    assert_blind,
    discover_ecommerce_truth_files,
    expected_from_truth,
    load_truth,
    rca_input_from_truth,
    triage_verdict_from_truth,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN = REPO_ROOT / "agents" / "rca_agent" / "evals" / "golden.json"


def _truth_files():
    paths = discover_ecommerce_truth_files()
    assert paths, "no ecommerce truth files found — the RCA eval set is missing"
    return paths


def _all_keys(value) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            found.add(str(key))
            found |= _all_keys(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            found |= _all_keys(child)
    return found


@pytest.mark.parametrize("path", _truth_files(), ids=lambda p: p.stem)
def test_rca_input_carries_no_truth_field(path):
    """No forbidden key appears at any depth of the agent's input."""
    truth = load_truth(path)
    payload = rca_input_from_truth(truth)
    leaked = _all_keys(payload) & FORBIDDEN_TRUTH_FIELDS
    assert not leaked, f"{path.stem}: RCA input leaked truth field(s) {sorted(leaked)}"


@pytest.mark.parametrize("path", _truth_files(), ids=lambda p: p.stem)
def test_rca_input_carries_no_answer_text(path):
    """The cause, the failure key and the remediation appear nowhere as text.

    Checked against the serialised payload rather than field by field, so a value
    copied into an unexpected place is still caught.
    """
    truth = load_truth(path)
    blob = json.dumps(rca_input_from_truth(truth), default=str).lower()
    for field in ("root_cause", "failure_key", "remediation"):
        value = str(truth.get(field) or "").strip().lower()
        if len(value) >= 12:  # distinctive enough to be the answer, not vocabulary
            assert value not in blob, f"{path.stem}: {field!r} value leaked into RCA input"


@pytest.mark.parametrize("path", _truth_files(), ids=lambda p: p.stem)
def test_rca_input_withholds_scenario_id(path):
    """``scenario_id`` is never passed.

    For the ecommerce suite its value is the failure key with the dot swapped for an
    underscore, and ``agent._fallback_verdict`` branches on it directly — so passing
    it would let the agent recognise the scenario under test and short-circuit to a
    hand-written verdict. A production alert webhook has no such field either.
    """
    payload = rca_input_from_truth(load_truth(path))
    assert "scenario_id" not in payload
    assert "scenario_id" not in payload["triage_verdict"]


@pytest.mark.parametrize("path", _truth_files(), ids=lambda p: p.stem)
def test_grading_key_does_carry_the_answer(path):
    """The harness side is *not* blind — otherwise nothing could be scored.

    The counterpart to the assertions above: it proves the split moved the truth
    somewhere rather than discarding it, so an empty grading key cannot masquerade as
    successful blinding.
    """
    truth = load_truth(path)
    expected = expected_from_truth(truth)
    assert expected["keywords"], f"{path.stem}: no grading keywords — nothing to score against"
    assert expected["service"], f"{path.stem}: no expected service"
    assert expected["failure_key"], f"{path.stem}: no expected failure key"


def test_assert_blind_detects_a_planted_field():
    """Positive control: the guard actually fires.

    A blindness check that never fails is indistinguishable from one that is not
    running, so the guard is tested against a deliberate leak.
    """
    with pytest.raises(TruthLeakError, match="root_cause"):
        assert_blind({"triage_verdict": {"root_cause": "MySQL is down"}})


def test_assert_blind_detects_a_planted_value():
    """The value check fires even when the field is innocently named."""
    truth = {"root_cause": "PostgreSQL database unavailable"}
    with pytest.raises(TruthLeakError, match="root_cause"):
        assert_blind(
            {"triage_verdict": {"alert_summary": "PostgreSQL database unavailable"}}, truth
        )


def test_assert_blind_permits_observable_text_sharing_answer_vocabulary():
    """An alert name is not a leak, even when it contains a cause keyword.

    ``EcommercePostgresDown`` is what a production SRE is paged with, and
    ``root_cause_keywords`` for that scenario contains ``postgres``. Rejecting the
    alert name would make the evaluation harder than reality rather than honest — and
    would push someone to disable the guard. This test pins that tolerance so a
    future tightening cannot silently remove it.
    """
    truth = load_truth(
        REPO_ROOT / "demo" / "ecommerce" / "truth_files" / "order_service_postgres_down.json"
    )
    payload = rca_input_from_truth(truth)
    assert "EcommercePostgresDown" in payload["triage_verdict"]["alert_summary"]


def test_latency_scenario_is_not_a_false_positive():
    """``fault_category`` of ``"latency"`` must not trip the value check.

    The generic single-word category legitimately appears inside the observable
    metric name ``order_latency_seconds``. This is the exact false positive that
    ``_is_distinctive`` exists for, and it is asserted so the rule is not "simplified"
    back into a bug.
    """
    truth = load_truth(
        REPO_ROOT / "demo" / "ecommerce" / "truth_files" / "user_service_high_latency.json"
    )
    assert truth["fault_category"] == "latency"
    payload = rca_input_from_truth(truth)  # must not raise
    assert "latency" in json.dumps(payload).lower()


@pytest.mark.parametrize("path", _truth_files(), ids=lambda p: p.stem)
def test_triage_verdict_is_shaped_like_ra001_output(path):
    """The synthesised verdict has the fields RCA actually reads.

    Guards a silent regression where a renamed key would make RCA reason from
    ``service="unknown"`` and an empty summary, which looks like an accuracy problem
    rather than an adapter bug.
    """
    verdict = triage_verdict_from_truth(load_truth(path))
    assert verdict["affected_service"]
    assert verdict["severity"].startswith("Sev-")
    assert verdict["alert_summary"]
    assert verdict["audit_metadata"]["decision_trace"]


def test_checked_in_golden_is_blind():
    """The committed golden file contains no truth content.

    ``golden.json`` is generated through ``rca_input_from_truth``, but it is a file on
    disk that someone can hand-edit — pasting a root cause into an ``expected`` block
    while debugging is exactly the kind of well-meant edit this catches.
    """
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    cases = doc["cases"]
    assert len(cases) == len(_truth_files()), "golden is out of sync with the truth-file set"
    for case in cases:
        leaked = _all_keys(case["input"]) & FORBIDDEN_TRUTH_FIELDS
        assert not leaked, f"{case['id']}: golden input leaked {sorted(leaked)}"


def test_synthetic_context_carries_no_truth_field():
    """The simulated telemetry is observable-only too.

    The synthetic Context Pack is the other thing handed to the agent, so it needs the
    same guarantee as the verdict — and it is built from ``expected_signals``, which
    lives right beside ``root_cause`` in the same file.
    """
    from evals.rca_synthetic import build_synthetic_context

    for path in _truth_files():
        truth = load_truth(path)
        pack, _synthetic = build_synthetic_context(truth)
        dumped = pack.model_dump(mode="json")
        leaked = _all_keys(dumped) & FORBIDDEN_TRUTH_FIELDS
        assert not leaked, f"{path.stem}: synthetic context leaked {sorted(leaked)}"
        blob = json.dumps(dumped, default=str).lower()
        cause = str(truth.get("root_cause") or "").lower()
        if len(cause) >= 12:
            assert cause not in blob, f"{path.stem}: root_cause leaked into synthetic telemetry"


def test_incident_history_section_is_withheld_from_synthetic_context():
    """RCA is given no incident history in evaluation.

    ``aiops/tools/incident_history/corpus.py`` builds its corpus from these very truth
    files, mapping ``recorded_cause = truth["root_cause"]``. So a retrieval against it
    during an evaluation of scenario X returns X's own answer at near-perfect
    similarity. Until outcome-backed memory exists (Phase 3), the section stays
    withheld, and this test is what stops it being switched on by accident.
    """
    from aiops.context.models import SectionStatus
    from evals.rca_synthetic import build_synthetic_context

    pack, _ = build_synthetic_context(load_truth(_truth_files()[0]))
    assert pack.incident_history.status is SectionStatus.NOT_REQUESTED
    assert not pack.incident_history.observations


def test_golden_inputs_have_not_drifted_from_the_adapter():
    """The golden's frozen inputs still match what the adapter produces today.

    ``golden.json`` is generated once and committed, so it is a *snapshot*. If someone
    changes ``triage_verdict_from_truth`` — a different severity mapping, an extra
    trace line — the committed golden keeps testing the old shape, and the two drift
    apart silently: the CI gate stays green while measuring an input the agent no
    longer receives in production.

    Cheap to check and it needs no LLM, unlike the end-to-end truth-file path, which is
    why this is the primary drift guard rather than opting all 12 scenarios into
    ``exercises`` and running RCA twice per harness invocation for the same assertions.
    """
    doc = json.loads(GOLDEN.read_text(encoding="utf-8"))
    by_scenario = {case["scenario"]: case["input"] for case in doc["cases"]}
    for path in _truth_files():
        expected_input = rca_input_from_truth(load_truth(path))
        assert path.stem in by_scenario, f"{path.stem} has no golden case"
        assert by_scenario[path.stem] == expected_input, (
            f"{path.stem}: golden input has drifted from evals.rca_truth. "
            "Regenerate agents/rca_agent/evals/golden.json rather than hand-editing it."
        )


def test_harness_can_drive_rca_agent_from_a_truth_file(tmp_path, monkeypatch):
    """``evals.harness`` can run rca_agent through the blind adapter.

    Uses a temporary truth file rather than opting a real one into its ``exercises``
    block. Doing the latter made every test that calls ``run_truth_file()`` run RCA,
    whose evidence gathering reaches the live observability seam — against a real
    ``AIOPS_PROMETHEUS_URL`` from ``.env`` that hangs and trips the 60s per-test cap.
    A shared fixture file should not decide whether the suite touches the network.

    Retrieval is neutralised here for the same reason: this asserts the adapter is
    wired and produces a scoreable verdict, not what the telemetry happened to say.
    """
    from agents.rca_agent import agent as rca
    from evals import harness

    monkeypatch.setattr(rca._evidence, "gather", lambda *a, **k: {})
    monkeypatch.setattr(rca, "_fetch_change_evidence", lambda *a, **k: None)

    truth = {
        "id": "harness_adapter_probe",
        "service": "order-service",
        "expected_alert_payload": {
            "alert_id": "ALT-probe",
            "service": "order-service",
            "metric": "postgres_connection_status",
            "value": 0.0,
            "threshold": 1.0,
            "severity_hint": "critical",
            "timestamp": "2026-08-03T10:00:00Z",
            "source": "Prometheus",
            "labels": {"alertname": "EcommercePostgresDown", "severity": "critical"},
        },
        "exercises": {"rca_agent": {"affected_service": "order-service"}},
    }
    path = tmp_path / "harness_adapter_probe.json"
    path.write_text(json.dumps(truth), encoding="utf-8")

    result = harness.run_truth_file(path)

    assert "rca_agent" not in result.deferred, "rca_agent should be runnable, not deferred"
    assert len(result.results) == 1
    assert result.results[0].case_id.endswith("::rca_agent")
    assert result.results[0].passed, result.results[0].details
