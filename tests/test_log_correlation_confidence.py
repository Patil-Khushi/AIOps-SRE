"""Tests for confidence explainability (Phase 6).

The brief's hard constraint is that the *number* must not change — only its
explanation. So the load-bearing test here is
``test_score_matches_the_original_algorithm_exhaustively``: it reimplements the
pre-refactor arithmetic verbatim and compares against the new implementation
across every combination of inputs. Anything else could pass while the score
silently drifted.

Two design points the rest of the tests pin down:

- ``confidence`` and ``confidence_breakdown.score`` come from **one**
  implementation. Duplicating the rules and trusting a test to catch divergence
  would be strictly worse than making divergence impossible.
- The algorithm has no negative terms, so "deductions" is not padded with
  invented ones. The cap is the single real deduction; rules that did not fire are
  reported as *unapplied*, which is usually the more useful half — it names the
  evidence that would raise the score.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from agents.log_correlation import CorrelationInput, correlate
from agents.log_correlation.confidence import (
    BASE_SCORE,
    MAX_SCORE,
    NO_SIGNAL_SCORE,
    RULE_DELTAS,
    ConfidenceBreakdown,
    explain_confidence,
)
from agents.log_correlation.models import CorrelatedSignal

_ERR = {"error", "critical", "fatal", "warn", "warning"}


def _signal(severity: str = "error") -> CorrelatedSignal:
    return CorrelatedSignal(
        source="logs", signature="sig", timestamp=datetime.now(UTC), severity=severity
    )


def _window(minutes: int = 15) -> dict[str, str]:
    end = datetime.now(UTC)
    return {"start": (end - timedelta(minutes=minutes)).isoformat(), "end": end.isoformat()}


def _original_confidence(signal_counts, top_signatures, suspects, first_error, cross_source):
    """The pre-refactor implementation, reproduced verbatim.

    Kept as the oracle rather than asserting hand-computed constants: if the new
    code drifts, this shows it immediately and in terms of the algorithm rather
    than of a magic number someone has to re-derive.
    """
    n_sources = sum(1 for v in signal_counts.values() if v > 0)
    total = sum(signal_counts.values())
    if total == 0:
        return 0.1
    score = 0.3
    if n_sources >= 2:
        score += 0.2
    if n_sources >= 3:
        score += 0.15
    if cross_source:
        score += 0.1
    if first_error is not None and first_error.severity.lower() in _ERR:
        score += 0.15
    if suspects:
        score += 0.1
    return round(min(score, 0.95), 3)


# ─── the number must not change ──────────────────────────────────────────────


def test_score_matches_the_original_algorithm_exhaustively():
    """Every input combination must produce the identical score.

    This is the test the whole phase rests on: explainability is worthless if it
    moved the number it explains.
    """
    counts = [
        {"logs": 0, "traces": 0, "metrics": 0},
        {"logs": 5, "traces": 0, "metrics": 0},
        {"logs": 5, "traces": 3, "metrics": 0},
        {"logs": 5, "traces": 3, "metrics": 2},
        {"logs": 1, "traces": 0, "metrics": 1},
    ]
    firsts = [None, _signal("error"), _signal("info"), _signal("critical"), _signal("warn")]
    suspect_sets = [[], ["payment"], ["payment", "cart"]]

    checked = 0
    for sc, fe, su, cs in itertools.product(counts, firsts, suspect_sets, [True, False]):
        expected = _original_confidence(sc, ["sig"], su, fe, cs)
        actual = explain_confidence(sc, ["sig"], su, fe, cs, error_severities=_ERR).score
        assert actual == expected, f"drift: counts={sc} first={fe} suspects={su} cross={cs}"
        checked += 1
    assert checked == 150, f"expected full matrix, checked {checked}"


def test_no_signal_floor_is_preserved():
    b = explain_confidence(
        {"logs": 0, "traces": 0, "metrics": 0}, [], [], None, False, error_severities=_ERR
    )
    assert b.score == NO_SIGNAL_SCORE
    assert b.contributors == []
    assert len(b.unapplied) == len(RULE_DELTAS), "every rule should be reported as unevaluated"


def test_cap_is_enforced_and_reported_as_a_deduction():
    """The only genuine deduction the algorithm has."""
    b = explain_confidence(
        {"logs": 5, "traces": 3, "metrics": 2},
        ["sig"],
        ["payment"],
        _signal("error"),
        True,
        error_severities=_ERR,
    )
    assert b.raw_total == 1.0
    assert b.score == MAX_SCORE
    assert b.capped is True
    assert len(b.deductions) == 1
    assert b.deductions[0].rule_id == "max_score_cap"
    assert b.deductions[0].delta < 0


def test_uncapped_score_reports_no_deductions():
    b = explain_confidence(
        {"logs": 5, "traces": 0, "metrics": 0},
        ["sig"],
        [],
        _signal("info"),
        False,
        error_severities=_ERR,
    )
    assert b.capped is False
    assert b.deductions == []


def test_arithmetic_is_auditable_from_the_breakdown():
    """base + contributors + deductions must reproduce the score, so a reader can
    verify the number rather than trust it."""
    b = explain_confidence(
        {"logs": 5, "traces": 3, "metrics": 2},
        ["sig"],
        ["payment"],
        _signal("error"),
        True,
        error_severities=_ERR,
    )
    recomputed = round(
        b.base + sum(c.delta for c in b.contributors) + sum(d.delta for d in b.deductions), 3
    )
    assert recomputed == b.score


# ─── one implementation, no divergence ───────────────────────────────────────


def test_agent_confidence_delegates_to_the_explainer():
    """``_confidence`` must not carry its own copy of the rules."""
    from agents.log_correlation import agent as lc_agent

    counts = {"logs": 5, "traces": 3, "metrics": 0}
    direct = lc_agent._confidence(counts, ["sig"], ["payment"], _signal("error"), False)
    explained = lc_agent._confidence_breakdown(
        counts, ["sig"], ["payment"], _signal("error"), False
    )
    assert direct == explained.score


def test_result_confidence_equals_breakdown_score():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    assert r.confidence == r.confidence_breakdown.score


# ─── each increment explains itself ──────────────────────────────────────────


def test_every_contributor_names_its_rule_and_reason():
    b = explain_confidence(
        {"logs": 5, "traces": 3, "metrics": 2},
        ["sig"],
        ["payment"],
        _signal("error"),
        True,
        error_severities=_ERR,
    )
    for c in b.contributors:
        assert c.rule_id in RULE_DELTAS, "rule id must be one of the known rules"
        assert c.delta == RULE_DELTAS[c.rule_id], "delta must match the rule's constant"
        assert len(c.description) > 20, "description must explain, not label"


def test_contributors_are_linked_to_the_evidence_that_triggered_them():
    """A score increment a reader cannot trace to a log line is not an
    explanation."""
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    b = r.confidence_breakdown
    evidence_ids = {e.evidence_id for e in r.evidence}

    linked = [c for c in b.contributors if c.triggered_by]
    assert linked, "at least one contributor should cite evidence"
    for c in linked:
        assert set(c.triggered_by) <= evidence_ids, "cited ids must exist in the evidence"


def test_unapplied_rules_state_why_and_what_was_forgone():
    """The more useful half: naming the missing evidence."""
    b = explain_confidence(
        {"logs": 5, "traces": 0, "metrics": 0},
        ["sig"],
        [],
        _signal("info"),
        False,
        error_severities=_ERR,
    )
    ids = {u.rule_id for u in b.unapplied}
    assert {"multi_source", "tri_source", "cross_source_recurrence"} <= ids
    for u in b.unapplied:
        assert len(u.reason) > 10
        assert u.potential_delta == RULE_DELTAS[u.rule_id]


def test_single_source_explanation_names_the_gap():
    b = explain_confidence(
        {"logs": 5, "traces": 0, "metrics": 0}, ["sig"], [], None, False, error_severities=_ERR
    )
    reason = next(u.reason for u in b.unapplied if u.rule_id == "multi_source")
    assert "1 signal source" in reason


def test_rule_trace_records_every_evaluation():
    b = explain_confidence(
        {"logs": 5, "traces": 3, "metrics": 0},
        ["sig"],
        ["payment"],
        _signal("error"),
        False,
        error_severities=_ERR,
    )
    joined = " ".join(b.rule_trace)
    for rule in RULE_DELTAS:
        assert rule in joined, f"{rule} missing from the trace"
    assert "final=" in joined


def test_explanation_is_human_readable():
    b = explain_confidence(
        {"logs": 5, "traces": 3, "metrics": 0},
        ["sig"],
        ["payment"],
        _signal("error"),
        False,
        error_severities=_ERR,
    )
    assert str(b.score) in b.explanation
    assert "base" in b.explanation
    assert "did not apply" in b.explanation


@pytest.mark.parametrize(
    ("severity", "should_apply"),
    [("error", True), ("critical", True), ("warn", True), ("info", False), ("debug", False)],
)
def test_error_severity_rule_follows_the_agent_vocabulary(severity, should_apply):
    """The rule must use the agent's own ``_ERROR_SEVERITIES`` set, not a private
    copy that could drift from it."""
    b = explain_confidence(
        {"logs": 5, "traces": 0, "metrics": 0},
        ["sig"],
        [],
        _signal(severity),
        False,
        error_severities=_ERR,
    )
    applied = {c.rule_id for c in b.contributors}
    assert ("error_severity_first" in applied) is should_apply


# ─── immutability and serialization ──────────────────────────────────────────


def test_breakdown_is_immutable():
    b = explain_confidence(
        {"logs": 1, "traces": 0, "metrics": 0}, [], [], None, False, error_severities=_ERR
    )
    with pytest.raises(Exception):
        b.score = 0.99


def test_breakdown_is_json_serializable():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    dumped = r.model_dump(mode="json")["confidence_breakdown"]

    for field in (
        "score",
        "base",
        "explanation",
        "contributors",
        "deductions",
        "unapplied",
        "rule_trace",
    ):
        assert field in dumped
    assert dumped["score"] == r.confidence


def test_breakdown_rejects_unknown_fields():
    with pytest.raises(Exception):
        ConfidenceBreakdown(score=0.5, explanation="x", bogus=1)


# ─── backward compatibility ──────────────────────────────────────────────────


def test_existing_outputs_unchanged():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)

    assert r.confidence == 0.9, "the eval-asserted score must not move"
    assert r.suspected_dependencies == ["payment"]
    assert len(r.timeline) == 3
    assert r.evidence
    assert r.incident_timeline is not None


def test_breakdown_is_optional_on_the_model():
    from agents.log_correlation.models import AuditMetadata, CorrelationResult

    r = CorrelationResult(
        service="x",
        summary="y",
        confidence=0.5,
        audit_metadata=AuditMetadata(created_at=datetime.now(UTC)),
    )
    assert r.confidence_breakdown is None


def test_confidence_works_without_evidence():
    """Scoring must not depend on evidence — evidence only supplies ids for the
    explanation, so a failed evidence build cannot change the number."""
    counts = {"logs": 5, "traces": 3, "metrics": 0}
    with_ev = explain_confidence(
        counts, ["sig"], ["payment"], _signal("error"), False, error_severities=_ERR, evidence=None
    )
    with_empty = explain_confidence(
        counts, ["sig"], ["payment"], _signal("error"), False, error_severities=_ERR, evidence=[]
    )
    assert (
        with_ev.score
        == with_empty.score
        == _original_confidence(counts, ["sig"], ["payment"], _signal("error"), False)
    )


def test_malformed_evidence_does_not_break_scoring():
    """A bad evidence item must degrade the id links, not the score."""

    class _Bad:
        evidence_id = "x"

        def __getattr__(self, name):
            raise RuntimeError("boom")

    counts = {"logs": 5, "traces": 3, "metrics": 0}
    b = explain_confidence(
        counts,
        ["sig"],
        ["payment"],
        _signal("error"),
        False,
        error_severities=_ERR,
        evidence=[_Bad()],
    )
    assert b.score == _original_confidence(counts, ["sig"], ["payment"], _signal("error"), False)


def test_rca_agent_unaffected():
    from agents.rca_agent.agent import _render_evidence_block

    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    assert "payment" in _render_evidence_block(r.model_dump(mode="json"))


def test_base_constant_matches_the_original():
    assert BASE_SCORE == 0.3
    assert MAX_SCORE == 0.95
    assert NO_SIGNAL_SCORE == 0.1
    assert RULE_DELTAS == {
        "multi_source": 0.2,
        "tri_source": 0.15,
        "cross_source_recurrence": 0.1,
        "error_severity_first": 0.15,
        "suspects_identified": 0.1,
    }
