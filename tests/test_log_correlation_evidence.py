"""Tests for the structured evidence model (Phase 4).

Evidence is the first thing in this agent that another agent is meant to *reason
over* rather than display, so three properties matter more than the field list:

- **Immutability.** Evidence is a record handed to other agents. If a consumer
  could edit it, two agents reasoning about "the same" finding could see different
  content, and an audit trail that can be rewritten is not one.
- **Determinism.** ``run()`` forces the synthetic path so the golden eval is a
  reproducible regression test. Random UUIDs for ``evidence_id`` would break that
  and make two runs of one incident incomparable.
- **Additivity.** Every pre-existing ``CorrelationResult`` field must be
  untouched, and the RCA agent — which reads the payload as a plain dict and pulls
  three keys by name — must not notice the new one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from agents.log_correlation import CorrelationInput, correlate
from agents.log_correlation.evidence import (
    Evidence,
    SupportingTelemetry,
    TopologyContext,
    make_correlation_id,
)
from agents.log_correlation.evidence_builder import (
    _classify,
    _evidence_confidence,
    _topology_context,
    build_evidence,
)
from agents.log_correlation.models import CorrelatedSignal


def _window(minutes: int = 15) -> dict[str, str]:
    end = datetime.now(UTC)
    return {"start": (end - timedelta(minutes=minutes)).isoformat(), "end": end.isoformat()}


def _signal(source, signature, severity="error", sample="", offset=0) -> CorrelatedSignal:
    return CorrelatedSignal(
        source=source,
        signature=signature,
        timestamp=datetime.now(UTC) - timedelta(minutes=10) + timedelta(seconds=offset),
        severity=severity,
        sample=sample,
    )


def _payload(service="checkout"):
    return CorrelationInput(service=service, window=_window())


# ─── the 11 required fields ──────────────────────────────────────────────────


def test_evidence_carries_every_required_field():
    ev = build_evidence(
        _payload(), [_signal("logs", "boom", sample="level=error boom")], ["payment"]
    )
    assert len(ev) == 1
    e = ev[0]

    assert e.evidence_id and isinstance(e.evidence_id, str)
    assert e.correlation_id and isinstance(e.correlation_id, str)
    assert isinstance(e.timestamp, datetime)
    assert e.source == "logs"
    assert e.service == "checkout"
    assert e.signal_type == "error_log"
    assert e.normalized_signature == "boom"
    assert e.severity == "error"
    assert 0.0 <= e.confidence <= 1.0
    assert isinstance(e.supporting_telemetry, SupportingTelemetry)
    assert isinstance(e.topology_context, TopologyContext)


# ─── immutability ────────────────────────────────────────────────────────────


def test_evidence_cannot_be_mutated():
    e = build_evidence(_payload(), [_signal("logs", "boom")], [])[0]
    with pytest.raises(Exception):
        e.severity = "info"


def test_nested_models_are_also_immutable():
    """Freezing only the outer model would leave the proof editable while the
    claim was protected."""
    e = build_evidence(_payload(), [_signal("logs", "boom")], [])[0]
    with pytest.raises(Exception):
        e.supporting_telemetry.occurrences = 999
    with pytest.raises(Exception):
        e.topology_context.relation = "self"


def test_evidence_rejects_unknown_fields():
    with pytest.raises(Exception):
        Evidence(
            evidence_id="a",
            correlation_id="b",
            timestamp=datetime.now(UTC),
            source="logs",
            service="checkout",
            signal_type="error_log",
            normalized_signature="x",
            severity="error",
            confidence=0.5,
            supporting_telemetry=SupportingTelemetry(),
            topology_context=TopologyContext(),
            bogus=1,
        )


# ─── deterministic identifiers ───────────────────────────────────────────────


def test_ids_are_deterministic_for_identical_input():
    """A random UUID would make the golden eval non-reproducible and two runs of
    the same incident incomparable."""
    p = _payload()
    sig = [_signal("logs", "boom")]
    first = build_evidence(p, sig, ["payment"])
    second = build_evidence(p, sig, ["payment"])

    assert [e.evidence_id for e in first] == [e.evidence_id for e in second]
    assert first[0].correlation_id == second[0].correlation_id


def test_correlation_id_is_shared_across_one_incident():
    ev = build_evidence(
        _payload(),
        [_signal("logs", "a"), _signal("traces", "b"), _signal("metrics", "c")],
        [],
    )
    assert len({e.correlation_id for e in ev}) == 1


def test_correlation_id_derives_from_the_incident_not_the_clock():
    """Same service + window must yield the same id, so a re-correlation can be
    compared with its predecessor rather than merely stacked next to it."""
    a = make_correlation_id("checkout", "2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00")
    b = make_correlation_id("Checkout", "2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00")
    c = make_correlation_id("cart", "2026-01-01T00:00:00+00:00", "2026-01-01T00:15:00+00:00")

    assert a == b, "service name should normalise"
    assert a != c


def test_evidence_ids_are_unique_within_one_pack():
    ev = build_evidence(
        _payload(), [_signal("logs", "a"), _signal("traces", "a"), _signal("logs", "b")], []
    )
    assert len({e.evidence_id for e in ev}) == len(ev)


def test_ordering_is_deterministic():
    p = _payload()
    sigs = [_signal("logs", "a"), _signal("metrics", "b", severity="info"), _signal("traces", "a")]
    order = [e.evidence_id for e in build_evidence(p, sigs, [])]
    for _ in range(3):
        assert [e.evidence_id for e in build_evidence(p, sigs, [])] == order


# ─── grouping ────────────────────────────────────────────────────────────────


def test_repeated_signals_collapse_into_one_evidence_with_a_count():
    """Fifty identical lines are one finding seen fifty times; emitting fifty
    near-identical objects would bury the distinct findings."""
    sigs = [_signal("logs", "same", offset=i) for i in range(50)]
    ev = build_evidence(_payload(), sigs, [])

    assert len(ev) == 1
    assert ev[0].supporting_telemetry.occurrences == 50


def test_same_signature_from_different_sources_stays_separate():
    ev = build_evidence(_payload(), [_signal("logs", "same"), _signal("traces", "same")], [])
    assert len(ev) == 2
    assert {e.source for e in ev} == {"logs", "traces"}


def test_cross_source_agreement_is_recorded_on_each_evidence():
    ev = build_evidence(_payload(), [_signal("logs", "same"), _signal("traces", "same")], [])
    for e in ev:
        assert e.supporting_telemetry.sources_agreeing == ["logs", "traces"]


def test_first_and_last_seen_span_the_group():
    sigs = [_signal("logs", "same", offset=i * 10) for i in range(4)]
    e = build_evidence(_payload(), sigs, [])[0]
    assert e.supporting_telemetry.first_seen < e.supporting_telemetry.last_seen


def test_strongest_severity_in_a_group_wins():
    """A signature that ever produced an error must not be downgraded because
    later occurrences were INFO."""
    sigs = [
        _signal("logs", "same", severity="info", offset=0),
        _signal("logs", "same", severity="error", offset=5),
        _signal("logs", "same", severity="info", offset=10),
    ]
    assert build_evidence(_payload(), sigs, [])[0].severity == "error"


# ─── signal type classification ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "severity", "signature", "expected"),
    [
        ("logs", "error", "x", "error_log"),
        ("logs", "critical", "x", "error_log"),
        ("logs", "warning", "x", "warning_log"),
        ("logs", "info", "x", "log_line"),
        ("traces", "error", "span", "error_span"),
        ("traces", "info", "GetProduct span ~500ms", "slow_span"),
        ("traces", "info", "plain", "trace_summary"),
        ("metrics", "error", "x", "metric_anomaly"),
        ("metrics", "info", "x", "metric_sample"),
    ],
)
def test_classification(source, severity, signature, expected):
    assert _classify(_signal(source, signature, severity=severity)) == expected


def test_is_failure_distinguishes_faults_from_context():
    err = build_evidence(_payload(), [_signal("logs", "x", severity="error")], [])[0]
    info = build_evidence(_payload(), [_signal("logs", "y", severity="info")], [])[0]
    assert err.is_failure is True
    assert info.is_failure is False


# ─── per-evidence confidence ─────────────────────────────────────────────────


def test_cross_source_agreement_raises_confidence_most():
    """Agreement is the strongest correlation rule the agent has, so the
    per-item score must agree with it."""
    one = _evidence_confidence(
        severity="error", sources_agreeing=1, occurrences=1, topology_depth=None
    )
    three = _evidence_confidence(
        severity="error", sources_agreeing=3, occurrences=1, topology_depth=None
    )
    assert three > one


def test_error_severity_beats_info():
    err = _evidence_confidence(
        severity="error", sources_agreeing=1, occurrences=1, topology_depth=None
    )
    info = _evidence_confidence(
        severity="info", sources_agreeing=1, occurrences=1, topology_depth=None
    )
    assert err > info


def test_confidence_never_claims_certainty():
    """Heuristic evidence should never assert 1.0."""
    top = _evidence_confidence(
        severity="critical", sources_agreeing=3, occurrences=99, topology_depth=1
    )
    assert top <= 0.95


def test_per_evidence_confidence_can_differ_from_the_verdict():
    """The point of the field: a corroborated finding and a lone INFO line must
    not inherit the same number from the aggregate verdict."""
    sigs = [
        _signal("logs", "strong", severity="error"),
        _signal("traces", "strong", severity="error"),
        _signal("metrics", "weak", severity="info"),
    ]
    ev = {e.normalized_signature: e for e in build_evidence(_payload(), sigs, [])}
    assert ev["strong"].confidence > ev["weak"].confidence


# ─── topology context ────────────────────────────────────────────────────────


def test_evidence_naming_a_dependency_implicates_it():
    """A checkout line reading "payment charge error" is evidence about payment,
    not about checkout — that distinction is the difference between "checkout is
    broken" and "checkout reports payment is broken"."""
    ctx = _topology_context("PlaceOrder failed: payment charge error", "", "checkout", ["payment"])
    assert ctx.relation == "dependency"
    assert ctx.implicated_service == "payment"
    assert ctx.depth == 1
    assert ctx.path == ["checkout", "payment"]


def test_evidence_naming_no_dependency_is_service_internal():
    ctx = _topology_context("internal handler panic", "", "checkout", ["payment", "cart"])
    assert ctx.relation == "self"
    assert ctx.implicated_service == "checkout"
    assert ctx.depth == 0


def test_absent_topology_stays_unknown_rather_than_guessing_self():
    """ "We could not place this" and "this is the service itself" are different
    claims; with no dependency list we are entitled only to the first."""
    ctx = _topology_context("something failed", "", "checkout", [])
    assert ctx.relation == "unknown"
    assert ctx.implicated_service is None


def test_only_known_dependencies_can_be_implicated():
    """Inference must not invent a service from arbitrary log text."""
    ctx = _topology_context("call to mystery-service failed", "", "checkout", ["payment"])
    assert ctx.implicated_service in (None, "checkout")
    assert ctx.implicated_service != "mystery-service"


def test_evidence_topology_agrees_with_the_verdict_suspects():
    """Both use the same dependency-name matching, so they must not drift."""
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    implicated = {
        e.topology_context.implicated_service
        for e in r.evidence
        if e.topology_context.relation == "dependency"
    }
    assert implicated <= set(r.suspected_dependencies) | {None}
    assert "payment" in implicated


# ─── additivity / backward compatibility ─────────────────────────────────────


def test_existing_result_fields_are_unchanged():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)

    assert r.service == "checkout"
    assert r.timeline, "timeline still carries the raw signals"
    assert r.top_signatures
    assert r.suspected_dependencies == ["payment"]
    assert r.confidence == 0.9, "aggregate verdict score must not move"
    assert r.audit_metadata.created_by == "RA-007"


def test_evidence_defaults_to_none_not_empty():
    """The default must be "no result", not "an empty result".

    Defaulting to ``[]`` made "stage 6 never ran" indistinguishable from "stage 6
    ran and derived nothing" — the ambiguity the rest of this result is built to
    avoid, in the field the PR leads with.
    """
    from agents.log_correlation.models import AuditMetadata, CorrelationResult

    r = CorrelationResult(
        service="x",
        summary="y",
        confidence=0.5,
        audit_metadata=AuditMetadata(created_at=datetime.now(UTC)),
    )
    assert r.evidence is None


def test_evidence_appears_in_the_json_payload():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    dumped = r.model_dump(mode="json")

    assert "evidence" in dumped
    assert dumped["evidence"], "should be populated when signals exist"
    assert set(dumped["evidence"][0]) == {
        "evidence_id",
        "correlation_id",
        "timestamp",
        "source",
        "service",
        "signal_type",
        "normalized_signature",
        "severity",
        "confidence",
        "supporting_telemetry",
        "topology_context",
    }


def test_rca_agent_still_renders_from_the_enriched_payload():
    """RCA reads three keys by name off a plain dict; a new key must be invisible."""
    from agents.rca_agent.agent import _render_evidence_block

    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    block = _render_evidence_block(r.model_dump(mode="json"))

    assert block
    assert "payment" in block


def test_rca_input_accepts_the_enriched_correlation_dict():
    from agents.rca_agent.models import RCAInput

    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    parsed = RCAInput(
        triage_verdict={"affected_service": "checkout"}, correlation=r.model_dump(mode="json")
    )
    assert parsed.correlation is not None


def test_run_shim_includes_evidence_and_stays_deterministic():
    from agents.log_correlation import run

    first = run({"service": "cart", "window": _window()})
    assert "evidence" in first
    assert first["audit_metadata"]["signal_source"] == "synthetic"


# ─── robustness ──────────────────────────────────────────────────────────────


def test_no_signals_yields_no_evidence():
    assert build_evidence(_payload(), [], ["payment"]) == []


def test_evidence_build_failure_does_not_lose_the_verdict(monkeypatch):
    """Evidence is an enrichment, not a prerequisite: a builder bug must not
    discard an otherwise complete verdict."""
    from agents.log_correlation import agent as lc_agent

    def _boom(*_a, **_kw):
        raise RuntimeError("builder exploded")

    monkeypatch.setattr(lc_agent, "build_evidence", _boom)
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)

    # None, not []: a caught exception must not be reachable by the same value a
    # legitimate "ran, nothing to derive" result produces. Asserting == [] here is
    # what let the ambiguity ship.
    assert r.evidence is None
    assert r.suspected_dependencies == ["payment"], "verdict survives intact"
    assert any("evidence: build failed" in t for t in r.audit_metadata.decision_trace)
