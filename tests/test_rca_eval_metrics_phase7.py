"""Phase 7 — the scorers that turned three ``PENDING_METRICS`` entries into real numbers.

Why category accuracy, specifically
------------------------------------
Every truth file carries ``grading.must_identify_category`` — a direct label — and it sat
unused since Phase 1. ``root_cause_accuracy`` matches keywords against prose and accepts
any one synonym, which the Phase 1 checkpoint already named an *upper bound*, not an
estimate. Category accuracy is the check against that upper bound: when the two disagree,
the keyword metric is the one to distrust.

The alias table's own history, pinned rather than narrated
------------------------------------------------------------
The first version of ``CATEGORY_ALIASES`` assumed ``Hypothesis.category`` was the catalog's
``rule_id`` and aliased toward it — wrong: only 4 of the 10 catalog rules have a
``category`` identical to their ``rule_id``. The mistake still surfaced correctly-looking
numbers, because the twelve truth files were authored against the *category* vocabulary
directly, and most already agreed with no table at all. ``TestCategoryVocabularyMatchesNatively``
is the regression test for that specific way of being fooled — checked against what the
truth files actually match, not against catalog internals a wrong premise made look right.
"""

from __future__ import annotations

from evals import rca_metrics as m


def _truth_file_categories() -> list[str]:
    from evals.rca_truth import discover_ecommerce_truth_files, load_truth

    return [
        str((load_truth(p).get("grading") or {}).get("must_identify_category") or "")
        for p in discover_ecommerce_truth_files()
    ]


class TestCategoryVocabularyMatchesNatively:
    def test_most_truth_file_categories_need_no_alias_or_subtype_at_all(self):
        """The regression this pins, stated as the claim I actually verified — not the
        different claim ("``rule_id`` equals ``category``" on the catalog, true for only 4
        of 10 rules) that I first wrote here and that does not establish what this test
        needs. Most of the *twelve truth-file categories* match ``Hypothesis.category``
        with no table involved at all, which is what made "alias toward rule_id" look
        confirmed on the first, wrong attempt: the few genuine mismatches were absorbed
        without anyone checking whether the premise behind them was right.
        """
        from agents.rca_agent.investigation.catalog import RULES

        vocabulary = {r.category for r in RULES}
        categories = _truth_file_categories()
        assert categories, "expected ecommerce truth files to exist"
        native = sum(1 for c in categories if c in vocabulary)
        assert native >= 9, "if this drops, re-verify CATEGORY_ALIASES against real rules"

    def test_the_alias_table_is_small(self):
        """Kept deliberately tiny. A large table is a grader bent to agree with the system
        rather than a documented renaming of the same condition."""
        assert len(m.CATEGORY_ALIASES) <= 3

    def test_every_alias_target_is_a_real_category(self):
        """An alias pointing at a category no rule produces would silently score nothing."""
        from agents.rca_agent.investigation.catalog import RULES

        real = {r.category for r in RULES}
        for target in m.CATEGORY_ALIASES.values():
            assert target in real, target

    def test_every_subtype_target_is_a_real_category(self):
        from agents.rca_agent.investigation.catalog import RULES

        real = {r.category for r in RULES}
        for target in m.CATEGORY_SUBTYPES.values():
            assert target in real, target


class TestNormaliseCategory:
    def test_unaliased_values_pass_through(self):
        assert m.normalise_category("dependency_unavailable") == "dependency_unavailable"

    def test_a_documented_alias_maps_across(self):
        assert m.normalise_category("startup_config_error") == "startup_failure"

    def test_matching_is_case_and_space_insensitive(self):
        assert m.normalise_category("  Dependency_Unavailable  ") == "dependency_unavailable"

    def test_empty_input_is_empty_output(self):
        assert m.normalise_category("") == ""
        assert m.normalise_category(None) == ""  # type: ignore[arg-type]


class TestCategorySatisfies:
    def test_an_exact_match_satisfies(self):
        assert m.category_satisfies("dependency_unavailable", "dependency_unavailable")

    def test_a_documented_subtype_satisfies_the_general_label(self):
        """The memory-leak scenario asks for resource_exhaustion_memory; the investigation
        answering resource_exhaustion_memory_oom identified an OOM kill, which IS a memory
        exhaustion — a more precise answer, and marking it wrong would penalise precision."""
        assert m.category_satisfies("resource_exhaustion_memory_oom", "resource_exhaustion_memory")

    def test_the_relation_is_directional(self):
        """A general answer must NOT satisfy a specific label — nothing here lets a vague
        classification pass for a precise one."""
        assert not m.category_satisfies(
            "resource_exhaustion_memory", "resource_exhaustion_memory_oom"
        )

    def test_an_unrelated_category_does_not_satisfy(self):
        assert not m.category_satisfies("resource_saturation_cpu", "dependency_unavailable")

    def test_empty_expected_never_satisfies(self):
        """A truth file with no category label must not score as a free pass."""
        assert not m.category_satisfies("dependency_unavailable", "")

    def test_empty_actual_never_satisfies(self):
        assert not m.category_satisfies("", "dependency_unavailable")


def _verdict(
    *,
    root_cause: str = "payment-service cannot reach Redis. gauge=0",
    status: str = "confirmed",
    category: str = "dependency_unavailable",
    score: float = 0.82,
    second_category: str | None = "application_error",
    second_score: float = 0.47,
    supporting: tuple[str, ...] = ("redis_up: UNREACHABLE (gauge=0)",),
    events: int = 2,
) -> dict:
    matrices = [
        {
            "hypothesis": {"hypothesis_id": "h1", "category": category},
            "score": {"score": score},
            "supporting": [{"statement": s} for s in supporting],
            "contradicting": [],
            "checked_absent": [],
        }
    ]
    if second_category:
        matrices.append(
            {
                "hypothesis": {"hypothesis_id": "h2", "category": second_category},
                "score": {"score": second_score},
                "supporting": [],
                "contradicting": [],
                "checked_absent": [],
            }
        )
    return {
        "affected_service": "payment-service",
        "root_cause": root_cause,
        "confidence_score": score,
        "root_cause_status": status,
        "ranked_fix_steps": [],
        "investigation": {
            "matrices": matrices,
            "timeline": {"events": [{}] * events, "coverage_note": "sparse by design"},
            "historical_influence": {},
        },
    }


def _expected(*, category: str = "dependency_unavailable", keywords=("redis",)) -> dict:
    return {
        "scenario_id": "s1",
        "service": "payment-service",
        "category": category,
        "keywords": list(keywords),
        "failure_key": "",
        "root_cause": "",
        "signals": {},
    }


class TestCategoryScoring:
    def test_a_matching_category_scores_correct(self):
        score = m.score_scenario(expected=_expected(), verdict=_verdict())
        assert score.category_correct is True
        assert score.expected_category == "dependency_unavailable"
        assert score.actual_category == "dependency_unavailable"

    def test_a_different_category_scores_incorrect(self):
        score = m.score_scenario(
            expected=_expected(category="resource_saturation_cpu"), verdict=_verdict()
        )
        assert score.category_correct is False

    def test_an_abstention_is_never_counted_correct_even_with_the_right_class(self):
        """Mirrors ``root_cause_correct``'s own rule: an abstention is never a correct
        identification, however the classification would have read."""
        score = m.score_scenario(
            expected=_expected(), verdict=_verdict(status="uncertain"), evidence_coverage=1.0
        )
        assert score.abstained is True
        assert score.category_correct is False

    def test_a_subtype_answer_scores_correct_against_the_general_label(self):
        score = m.score_scenario(
            expected=_expected(category="resource_exhaustion_memory"),
            verdict=_verdict(category="resource_exhaustion_memory_oom", second_category=None),
        )
        assert score.category_correct is True

    def test_category_accuracy_is_reported_in_the_aggregate(self):
        report = m.MatrixReport(
            scenarios=[m.score_scenario(expected=_expected(), verdict=_verdict())]
        )
        assert report.category_accuracy == 1.0

    def test_mismatches_distinguish_abstention_from_wrong_class(self):
        report = m.MatrixReport(
            scenarios=[
                m.score_scenario(
                    expected=_expected(),
                    verdict=_verdict(status="uncertain"),
                    evidence_coverage=1.0,
                ),
                m.score_scenario(
                    expected=_expected(category="resource_saturation_cpu"), verdict=_verdict()
                ),
            ]
        )
        reasons = {row["reason"] for row in report.category_mismatches}
        assert "correct class, withheld as an abstention" in reasons
        assert "different class" in reasons


class TestEvidenceGroundingAndFabrication:
    def test_prose_that_restates_the_evidence_is_grounded(self):
        score = m.score_scenario(
            expected=_expected(),
            verdict=_verdict(root_cause="Redis is unreachable, gauge reads 0."),
        )
        assert score.evidence_grounded is True

    def test_prose_disconnected_from_the_evidence_is_not_grounded(self):
        score = m.score_scenario(
            expected=_expected(),
            verdict=_verdict(root_cause="The service is experiencing generic difficulties."),
        )
        assert score.evidence_grounded is False

    def test_with_no_investigation_grounding_is_none_not_false(self):
        verdict = _verdict()
        verdict["investigation"] = {}
        score = m.score_scenario(expected=_expected(), verdict=verdict)
        assert score.evidence_grounded is None

    def test_a_fabricated_metric_is_caught(self):
        """The system prompt's own words: the worst failure mode available, because an
        operator cannot tell an invented citation from a real one."""
        score = m.score_scenario(
            expected=_expected(),
            verdict=_verdict(
                root_cause="orders_failed_total shows reason=injected_500 on this service."
            ),
        )
        assert "orders_failed_total" in score.fabricated_citations

    def test_a_cited_metric_that_was_actually_observed_is_not_fabricated(self):
        score = m.score_scenario(
            expected=_expected(),
            verdict=_verdict(
                root_cause="redis_up shows unreachable.",
                supporting=("redis_up: UNREACHABLE (gauge=0)",),
            ),
        )
        assert score.fabricated_citations == ()

    def test_ordinary_words_are_not_mistaken_for_metrics(self):
        score = m.score_scenario(
            expected=_expected(), verdict=_verdict(root_cause="This is a straightforward issue.")
        )
        assert score.fabricated_citations == ()

    def test_the_grader_does_not_call_the_agents_own_grounding_check(self):
        """A grader that reused ``agent._grounded_in_investigation`` could not catch a bug
        in it — this module implements the check independently.

        Checked against the **AST's call targets**, not the module's text — a plain
        substring scan fails on this very docstring, which names the function it is
        making sure the code never calls. Same fix as the analogous mistake in
        ``tests/test_rca_learning.py``: a rule and a mention of the rule are not the same
        thing, and only an AST walk tells them apart.
        """
        import ast
        import inspect

        from evals import rca_metrics

        tree = ast.parse(inspect.getsource(rca_metrics))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        } | {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "_grounded_in_investigation" not in called

    def test_fabrication_rate_is_reported_in_the_aggregate(self):
        report = m.MatrixReport(
            scenarios=[
                m.score_scenario(
                    expected=_expected(),
                    verdict=_verdict(root_cause="orders_failed_total is moving."),
                )
            ]
        )
        assert report.fabricated_citation_rate == 1.0


class TestDiscriminationMargin:
    def test_the_margin_is_top_minus_runner_up(self):
        score = m.score_scenario(
            expected=_expected(), verdict=_verdict(score=0.82, second_score=0.47)
        )
        assert score.discrimination_margin == 0.35

    def test_a_single_candidate_has_no_margin(self):
        score = m.score_scenario(expected=_expected(), verdict=_verdict(second_category=None))
        assert score.discrimination_margin is None

    def test_the_aggregate_averages_available_margins(self):
        report = m.MatrixReport(
            scenarios=[
                m.score_scenario(
                    expected=_expected(), verdict=_verdict(score=0.8, second_score=0.4)
                ),
                m.score_scenario(expected=_expected(), verdict=_verdict(second_category=None)),
            ]
        )
        assert report.mean_discrimination_margin == 0.4


class TestTimelineCoverage:
    def test_events_are_counted_per_scenario(self):
        score = m.score_scenario(expected=_expected(), verdict=_verdict(events=3))
        assert score.timeline_events == 3

    def test_it_is_reported_as_coverage_not_accuracy(self):
        """No truth file records an expected timeline, so this is a count, never a
        pass/fail — the name says so and the value is a mean, not a rate."""
        report = m.MatrixReport(
            scenarios=[m.score_scenario(expected=_expected(), verdict=_verdict(events=4))]
        )
        assert report.timeline_coverage == 4.0

    def test_timeline_accuracy_stays_pending(self):
        """The field exists; the ground truth to score it against does not."""
        assert "timeline_accuracy" in m.PENDING_METRICS
        assert "NO TRUTH DATA" in m.PENDING_METRICS["timeline_accuracy"]


class TestBlastRadiusStillPending:
    def test_blast_radius_accuracy_is_pending_for_lack_of_truth_data_not_a_missing_field(self):
        """Distinguishes a genuinely blocked metric from a merely unwritten one — the
        report exists and is populated since Phase 5; nothing records an expected impact."""
        assert "blast_radius_accuracy" in m.PENDING_METRICS
        assert "NO TRUTH DATA" in m.PENDING_METRICS["blast_radius_accuracy"]

    def test_exactly_two_metrics_remain_pending(self):
        """Evidence grounding, hypothesis discrimination and timeline accuracy all got
        scorers in Phase 7. Only the two genuinely truth-blocked metrics remain."""
        assert set(m.PENDING_METRICS) == {"timeline_accuracy", "blast_radius_accuracy"}


class TestEndToEndAgreement:
    """The property that justifies leading with category accuracy: on the deterministic
    path, it should track root-cause accuracy almost exactly, because both are reading the
    same underlying classification through different lenses."""

    def test_category_and_root_cause_accuracy_track_each_other_on_the_deterministic_path(self):
        import os

        from evals.rca_eval import run_matrix

        os.environ["AIOPS_LLM_PROVIDER"] = "stub"
        report = run_matrix("baseline")
        assert abs(report.category_accuracy - report.root_cause_accuracy) <= 0.1
