"""Phase 6 — the closed loop: outcome recording, promotion on recurrence, and the
boundary the learning system may not cross.

Three claims:

1. **Only a verified recovery becomes recallable memory.** The verifier is the writer, and
   a FAIL is recorded too — as ``UNVERIFIED``, which can never influence a ranking.
2. **Trust is earned by recurrence**, counted from the store rather than tracked
   incrementally, so it cannot drift from what was actually recorded.
3. **Learning changes data, never code.** No prompt mutation, no source edits, no tool
   registration, no safety-rule changes. Asserted structurally, because a docstring
   promising it is not a control.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agents.rca_agent import learning
from agents.rca_agent.investigation import memory
from agents.rca_agent.investigation.models import MemoryStatus

LEARNING_SRC = pathlib.Path(learning.__file__).read_text(encoding="utf-8")


def _referenced_names(source: str) -> set[str]:
    """Every name the code actually references: identifiers, attributes, import targets.

    Docstrings and comments are excluded by construction, which is the point — a text scan
    cannot tell a rule from a mention of the rule.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.split(".")[-1])
    return names


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _store_verdict(
    incident_id: str = "INC-1",
    *,
    service: str = "payment-service",
    hypothesis_class: str = "dependency_unavailable",
    action_key: str | None = "payment_service.redis_down",
    status: str = "confirmed",
    confidence: float = 0.82,
    root_cause: str = "payment-service cannot reach Redis",
) -> None:
    """Persist an RCA verdict the way the fix-apply path does, so the learning module has
    something to close the loop on."""
    from aiops.state.repository import save_rca_result

    steps = []
    if action_key:
        steps.append(
            {
                "description": f"Clear {action_key}",
                "blast_radius": "low",
                "rollback": "undo",
                "requires_hitl": True,
                "action_type": "set_flag",
                "flag": action_key,
                "variant": "off",
            }
        )
    save_rca_result(
        incident_id=incident_id,
        affected_service=service,
        verdict={
            "affected_service": service,
            "root_cause": root_cause,
            "ranked_fix_steps": steps,
            "confidence_score": confidence,
            "root_cause_status": status,
            "investigation": {
                "scope": {"alert_name": "EcommerceRedisDown"},
                "selected_hypothesis_id": "hid-1",
                "matrices": [
                    {
                        "hypothesis": {
                            "hypothesis_id": "hid-1",
                            "category": hypothesis_class,
                        },
                        "supporting": [
                            {
                                "evidence_id": "e1",
                                "statement": "redis_up: UNREACHABLE (gauge=0)",
                            }
                        ],
                    }
                ],
            },
        },
    )


# ─── 1. only a verified recovery becomes memory ─────────────────────────────


class TestRecordVerifiedOutcome:
    def test_a_pass_becomes_verified(self):
        _store_verdict()
        row_id = learning.record_verified_outcome(
            "INC-1", service="payment-service", verification_result="resolved"
        )
        assert row_id is not None
        from aiops.state.repository import get_rca_outcome

        row = get_rca_outcome(row_id)
        assert row["memory_status"] == MemoryStatus.VERIFIED.value
        assert row["selected_hypothesis_class"] == "dependency_unavailable"
        assert row["action_key"] == "payment_service.redis_down"

    def test_a_fail_is_recorded_but_stays_unverified(self):
        """Recorded because an approved, executed prediction that did not work is the most
        informative record the system produces; unverified because it must never influence
        a later ranking."""
        _store_verdict()
        row_id = learning.record_verified_outcome(
            "INC-1", service="payment-service", verification_result="not_resolved"
        )
        assert row_id is not None
        from aiops.state.repository import get_rca_outcome

        assert get_rca_outcome(row_id)["memory_status"] == MemoryStatus.UNVERIFIED.value

    def test_a_failed_outcome_is_not_recallable(self):
        _store_verdict()
        learning.record_verified_outcome(
            "INC-1", service="payment-service", verification_result="not_resolved"
        )
        result = memory.recall(
            service="payment-service", signatures=["EcommerceRedisDown", "redis_up"]
        )
        assert result.priors == ()

    def test_a_verified_outcome_is_recallable(self, monkeypatch):
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "rca_outcomes")
        _store_verdict()
        learning.record_verified_outcome(
            "INC-1", service="payment-service", verification_result="resolved"
        )
        result = memory.recall(
            service="payment-service", signatures=["EcommerceRedisDown", "redis_up"]
        )
        assert [p.memory_id for p in result.priors] == ["INC-1"]

    def test_no_stored_verdict_records_nothing(self):
        """Common and not an error: an incident that never reached a proposed fix has no
        verdict to close the loop on."""
        assert (
            learning.record_verified_outcome("INC-nonexistent", verification_result="resolved")
            is None
        )

    def test_recording_never_raises(self, monkeypatch):
        import aiops.state.repository as repo

        monkeypatch.setattr(
            repo, "get_rca_result", lambda _i: (_ for _ in ()).throw(RuntimeError("db"))
        )
        assert learning.record_verified_outcome("INC-1", verification_result="resolved") is None

    def test_the_signatures_are_symptoms_not_the_cause(self):
        """A memory keyed on the answer would let a recall retrieve the priors that agree
        with a conclusion already reached and call it corroboration."""
        _store_verdict(root_cause="payment-service cannot reach Redis")
        row_id = learning.record_verified_outcome("INC-1", verification_result="resolved")
        from aiops.state.repository import get_rca_outcome

        signatures = get_rca_outcome(row_id)["signatures"]
        assert "EcommerceRedisDown" in signatures
        assert not any("cannot reach" in s for s in signatures)

    def test_the_prediction_is_preserved_verbatim(self):
        _store_verdict(root_cause="a very specific claim about Redis")
        row_id = learning.record_verified_outcome("INC-1", verification_result="resolved")
        from aiops.state.repository import get_rca_outcome

        assert (
            get_rca_outcome(row_id)["predicted_root_cause"] == "a very specific claim about Redis"
        )


# ─── 2. trust is earned by recurrence ───────────────────────────────────────


class TestPromotionOnRecurrence:
    def test_the_first_verified_outcome_is_verified_not_trusted(self):
        _store_verdict("INC-1")
        row_id = learning.record_verified_outcome("INC-1", verification_result="resolved")
        from aiops.state.repository import get_rca_outcome

        assert get_rca_outcome(row_id)["memory_status"] == MemoryStatus.VERIFIED.value

    def test_repeated_recurrence_earns_trust(self):
        from aiops.state.repository import get_rca_outcome

        last = None
        for i in range(memory.TRUST_THRESHOLD + 1):
            _store_verdict(f"INC-{i}")
            last = learning.record_verified_outcome(f"INC-{i}", verification_result="resolved")
        assert get_rca_outcome(last)["memory_status"] == MemoryStatus.TRUSTED.value

    def test_recurrence_is_counted_per_class(self):
        """A different failure class on the same service is not a recurrence of this one."""
        from aiops.state.repository import get_rca_outcome

        for i in range(memory.TRUST_THRESHOLD + 1):
            _store_verdict(f"INC-other-{i}", hypothesis_class="resource_saturation_cpu")
            learning.record_verified_outcome(f"INC-other-{i}", verification_result="resolved")
        _store_verdict("INC-new", hypothesis_class="dependency_unavailable")
        row_id = learning.record_verified_outcome("INC-new", verification_result="resolved")
        assert get_rca_outcome(row_id)["memory_status"] == MemoryStatus.VERIFIED.value

    def test_failed_outcomes_do_not_count_toward_trust(self):
        from aiops.state.repository import get_rca_outcome

        for i in range(memory.TRUST_THRESHOLD + 1):
            _store_verdict(f"INC-fail-{i}")
            learning.record_verified_outcome(f"INC-fail-{i}", verification_result="not_resolved")
        _store_verdict("INC-pass")
        row_id = learning.record_verified_outcome("INC-pass", verification_result="resolved")
        assert get_rca_outcome(row_id)["memory_status"] == MemoryStatus.VERIFIED.value


class TestCorrectionAndInvalidation:
    def test_a_correction_keeps_the_prediction(self):
        _store_verdict(root_cause="dns failure")
        row_id = learning.record_verified_outcome("INC-1", verification_result="not_resolved")
        row = learning.apply_human_correction(row_id, "redis was actually unreachable")
        assert row["predicted_root_cause"] == "dns failure"
        assert row["human_corrected_root_cause"] == "redis was actually unreachable"
        assert row["memory_status"] == MemoryStatus.VERIFIED.value

    def test_a_correction_counts_as_a_rejection_for_reliability(self, monkeypatch):
        """It teaches that the *predicted* class was wrong here, so the pattern's track
        record must get worse, not better."""
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "rca_outcomes")
        _store_verdict("INC-1")
        row_id = learning.record_verified_outcome("INC-1", verification_result="resolved")
        learning.apply_human_correction(row_id, "something else entirely")
        result = memory.recall(
            service="payment-service", signatures=["EcommerceRedisDown", "redis_up"]
        )
        # Reliability 0 of 1 → weight 0 → the prior contributes nothing.
        assert result.priors == ()

    def test_invalidation_retains_the_row(self):
        from aiops.state.repository import count_rca_outcomes, get_rca_outcome

        _store_verdict()
        row_id = learning.record_verified_outcome("INC-1", verification_result="resolved")
        learning.invalidate_outcome(row_id, reason="root cause was wrong")
        assert get_rca_outcome(row_id)["memory_status"] == MemoryStatus.INVALIDATED.value
        assert count_rca_outcomes() == 1

    def test_an_invalidated_row_is_not_recalled(self, monkeypatch):
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "rca_outcomes")
        _store_verdict()
        row_id = learning.record_verified_outcome("INC-1", verification_result="resolved")
        learning.invalidate_outcome(row_id)
        result = memory.recall(
            service="payment-service", signatures=["EcommerceRedisDown", "redis_up"]
        )
        assert result.priors == ()

    def test_correction_and_invalidation_are_safe_on_a_missing_row(self):
        assert learning.apply_human_correction(999999, "x") is None
        assert learning.invalidate_outcome(999999) is None


# ─── 3. learning changes data, never code ───────────────────────────────────


class TestLearningBoundary:
    """The constraint stated as a control rather than a promise.

    "The learning system must never modify RCA source code, prompts, executable
    remediation logic, tools, or safety rules automatically." A docstring saying so is not
    enforcement — these assertions are.
    """

    @pytest.mark.parametrize(
        "forbidden",
        [
            # Prompt and source mutation
            "SYSTEM_PROMPT_V7",
            "SYSTEM_PROMPT_V6",
            "write_text",
            "open",
            # Tool and policy surfaces
            "register_provider",
            "register_tool",
            "DEFAULT_LEVELS",
            # Dynamic execution
            "exec",
            "eval",
            "compile",
            "setattr",
            "__setattr__",
        ],
    )
    def test_the_learning_module_cannot_touch_code_prompts_or_policy(self, forbidden):
        """Checked against the module's **AST**, not its text.

        A substring scan failed on the word "prompts" inside the docstring that *explains*
        this very rule — the check was flagging its own documentation. Names actually
        referenced in code are what matter, so this walks imports, attribute accesses and
        call targets, the same AST discipline ``tests/test_layering.py`` uses.
        """
        assert forbidden not in _referenced_names(LEARNING_SRC), (
            f"learning.py references {forbidden!r} in code — learning may write outcome "
            "rows and nothing else"
        )

    def test_it_imports_no_prompt_or_policy_module(self):
        imported = _imported_modules(LEARNING_SRC)
        for module in imported:
            assert "prompts" not in module, module
            assert not module.startswith("aiops.policy"), module
            assert not module.startswith("aiops.tools"), module

    def test_it_writes_only_through_the_repository_and_the_memory_lifecycle(self):
        """Every write goes through ``memory.record_outcome`` (which calls ``promote``) or
        an explicit repository status update. There is no second write path that could
        insert a recallable row without a promotion decision."""
        assert "memory.record_outcome" in LEARNING_SRC
        assert "update_rca_outcome_memory_status" in LEARNING_SRC

    def test_it_does_not_decide_promotion_itself(self):
        """``verification_result`` is passed through to ``promote``; this module has no
        branch that sets a status from its own judgement of the outcome."""
        assert "MemoryStatus.TRUSTED" not in LEARNING_SRC
        assert "MemoryStatus.NEW" not in LEARNING_SRC

    def test_the_prompt_is_untouched_by_a_recorded_outcome(self):
        """End-to-end: record an outcome, then assert the system prompt is byte-identical."""
        from agents.rca_agent.prompts import SYSTEM_PROMPT_V7

        before = SYSTEM_PROMPT_V7
        _store_verdict()
        learning.record_verified_outcome("INC-1", verification_result="resolved")
        from agents.rca_agent.prompts import SYSTEM_PROMPT_V7 as after

        assert after == before


# ─── the verifier is the writer ─────────────────────────────────────────────


class TestVerifierIntegration:
    def test_the_verifier_records_on_pass(self, monkeypatch):
        recorded: list[tuple[str, str]] = []
        import agents.rca_agent.learning as learning_mod

        monkeypatch.setattr(
            learning_mod,
            "record_verified_outcome",
            lambda incident_id, **kw: recorded.append((incident_id, kw["verification_result"])),
        )
        from agents.resolution_verifier.verifier import VerifyContext, _record_rca_outcome

        _record_rca_outcome(
            VerifyContext(incident_id="INC-9", service="payment-service"), "resolved"
        )
        assert recorded == [("INC-9", "resolved")]

    def test_the_verifier_records_on_fail(self, monkeypatch):
        recorded: list[tuple[str, str]] = []
        import agents.rca_agent.learning as learning_mod

        monkeypatch.setattr(
            learning_mod,
            "record_verified_outcome",
            lambda incident_id, **kw: recorded.append((incident_id, kw["verification_result"])),
        )
        from agents.resolution_verifier.verifier import VerifyContext, _record_rca_outcome

        _record_rca_outcome(
            VerifyContext(incident_id="INC-9", service="payment-service"), "not_resolved"
        )
        assert recorded == [("INC-9", "not_resolved")]

    def test_a_recording_failure_cannot_break_a_closure(self, monkeypatch):
        """Bookkeeping must never cost the incident response that produced it."""
        import agents.rca_agent.learning as learning_mod

        def boom(*_a, **_k):
            raise RuntimeError("store on fire")

        monkeypatch.setattr(learning_mod, "record_verified_outcome", boom)
        from agents.resolution_verifier.verifier import VerifyContext, _record_rca_outcome

        _record_rca_outcome(VerifyContext(incident_id="INC-9", service="s"), "resolved")

    def test_nothing_else_writes_the_outcome_store_on_the_analysis_path(self):
        """Phase 3's invariant, still true: analysing writes no memory. Only the verifier
        does, and only after it has a verdict."""
        from aiops.state.repository import count_rca_outcomes

        before = count_rca_outcomes()
        from agents.rca_agent.agent import analyze

        analyze(
            {
                "affected_service": "payment-service",
                "alert_summary": "EcommerceRedisDown firing: cache down",
                "severity": "sev2",
            }
        )
        assert count_rca_outcomes() == before
