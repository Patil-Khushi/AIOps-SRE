"""Phase 3 blindness — the truth-file corpus must never reach RCA as historical memory.

The leak this file exists to prevent is concrete, not hypothetical. The platform's
default history provider searches ``aiops/tools/incident_history/corpus.py``, which loads
``demo/ecommerce/truth_files/*.json`` and maps each file's ``root_cause`` field onto
``ResolutionMetadata.recorded_cause``::

    "recorded_cause": data.get("root_cause"),

Those same twelve files are the RCA evaluation's graded answer key. So a recall over that
corpus, during an investigation of one of those scenarios, would hand the agent the exact
string it is about to be scored against — and the resulting accuracy would measure
lookup, not diagnosis. The number would go *up*, which is what makes this dangerous
rather than merely wrong.

Three independent guards, tested separately because any one of them could be removed by a
plausible-looking future change:

1. **An allowlist, not a chain.** ``OUTCOME_BACKED_PROVIDERS`` names the only providers a
   prior may be built from, so configuring a corpus-backed provider cannot produce one.
2. **A verified-only store.** The outcome provider emits nothing that a verifier has not
   confirmed, independently of what the caller asked for.
3. **Symptom-only queries.** A recall matches on alert names, metric names and reason
   codes — never on a cause description, and never on the agent's own hypotheses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agents.rca_agent.investigation import memory
from agents.rca_agent.investigation.models import MemoryStatus, RootCauseStatus

REPO_ROOT = Path(__file__).resolve().parent.parent
ECOMMERCE_TRUTH = REPO_ROOT / "demo" / "ecommerce" / "truth_files"
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _truth_files() -> list[Path]:
    if not ECOMMERCE_TRUTH.is_dir():  # pragma: no cover - repo layout guard
        return []
    return [p for p in sorted(ECOMMERCE_TRUTH.glob("*.json")) if not p.stem.startswith("_")]


def _truth_causes() -> list[str]:
    causes: list[str] = []
    for path in _truth_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover
            continue
        cause = str(data.get("root_cause") or "").strip()
        if cause:
            causes.append(cause)
    return causes


# ─── guard 1: the allowlist ─────────────────────────────────────────────────


class TestProviderAllowlist:
    def test_only_outcome_backed_providers_are_allowed(self):
        assert memory.OUTCOME_BACKED_PROVIDERS == frozenset({"rca_outcomes"})

    def test_the_corpus_backed_providers_are_all_excluded(self):
        """Named individually so adding a provider to the registry without deciding
        whether it may feed RCA memory shows up here."""
        from aiops.tools.incident_history.retriever import BUILTIN_PROVIDERS

        corpus_backed = {"mock", "embedding", "vector", "elastic", "postgres"}
        assert corpus_backed <= BUILTIN_PROVIDERS, "registry names drifted"
        assert not (corpus_backed & memory.OUTCOME_BACKED_PROVIDERS)

    def test_every_shipped_provider_is_either_allowed_or_deliberately_not(self):
        """A ratchet on the *shipped* registry: a new provider must be classified.

        Fails when someone adds a provider to ``retriever._PROVIDERS`` and neither puts it
        in the allowlist nor records it here as corpus-backed — the state in which a leak
        would arrive unnoticed.

        Reads ``BUILTIN_PROVIDERS``, not the live ``_PROVIDERS`` dict, and the difference
        is not cosmetic: the dict is module-global and mutable, ``register_provider`` is the
        documented way to add a backend, and ``test_incident_history.py`` leaves fakes named
        ``boom`` and ``badhealth`` in it. Reading the live dict made this test pass alone
        and fail in the full suite depending on ordering — and it was asserting the wrong
        thing anyway. A runtime-registered provider is the caller's business; RCA's
        allowlist refuses it whatever it is called. What needs governing is what this repo
        ships.
        """
        from aiops.tools.incident_history.retriever import BUILTIN_PROVIDERS

        known_corpus_backed = {"mock", "embedding", "vector", "elastic", "postgres"}
        unclassified = BUILTIN_PROVIDERS - known_corpus_backed - memory.OUTCOME_BACKED_PROVIDERS
        assert not unclassified, (
            f"new history provider(s) {sorted(unclassified)} must be classified: add to "
            "OUTCOME_BACKED_PROVIDERS only if they search verified outcomes, not truth files"
        )

    def test_a_runtime_registered_provider_cannot_feed_rca_memory(self):
        """The property that makes the ratchet's narrower scope safe.

        Whatever a caller registers at runtime, it is outside the allowlist and therefore
        refused — so restricting the ratchet to shipped providers gives up no protection.
        """

        class _Rogue:
            name = "rogue"

            def health(self):
                return True, "ok"

            def search(self, query):  # pragma: no cover - must never be called
                raise AssertionError("a rogue provider must never be searched for priors")

        from aiops.tools.incident_history import register_provider

        register_provider(_Rogue())
        try:
            allowed, _refused = memory.memory_providers()
            assert "rogue" not in allowed
            import os

            os.environ["AIOPS_RCA_MEMORY_PROVIDERS"] = "rogue"
            try:
                result = memory.recall(service="payment-service", signatures=["X"], now=NOW)
                assert result.status == "disabled"
                assert result.providers_refused == ("rogue",)
                assert result.priors == ()
            finally:
                os.environ.pop("AIOPS_RCA_MEMORY_PROVIDERS", None)
        finally:
            from aiops.tools.incident_history.retriever import _PROVIDERS

            _PROVIDERS.pop("rogue", None)

    @pytest.mark.parametrize("provider", ["mock", "embedding", "vector", "elastic", "postgres"])
    def test_configuring_a_corpus_provider_is_refused_not_honoured(self, provider, monkeypatch):
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", provider)
        allowed, refused = memory.memory_providers()
        assert allowed == ()
        assert refused == (provider,)

    def test_a_refused_provider_yields_no_priors_and_says_why(self, monkeypatch):
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "mock")
        result = memory.recall(service="payment-service", signatures=["PaymentRedisDown"], now=NOW)
        assert result.priors == ()
        assert result.status == "disabled"
        assert result.providers_refused == ("mock",)
        assert any("truth-file corpus" in note for note in result.notes)

    def test_mixing_an_allowed_and_a_refused_provider_keeps_only_the_allowed_one(self, monkeypatch):
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "mock,rca_outcomes,embedding")
        allowed, refused = memory.memory_providers()
        assert allowed == ("rca_outcomes",)
        assert set(refused) == {"mock", "embedding"}

    def test_the_default_is_the_outcome_store(self, monkeypatch):
        monkeypatch.delenv("AIOPS_RCA_MEMORY_PROVIDERS", raising=False)
        allowed, refused = memory.memory_providers()
        assert allowed == ("rca_outcomes",)
        assert refused == ()

    def test_an_explicit_empty_value_means_cold_start(self, monkeypatch):
        """Honoured as a deliberate choice rather than replaced by the default — this is
        the switch the cold-start evaluation arm flips."""
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "")
        assert memory.memory_providers() == ((), ())

    def test_the_platform_history_chain_is_unaffected(self, monkeypatch):
        """The allowlist constrains *RCA memory*, not the platform seam.

        Other consumers may legitimately search the truth-file corpus — it is real
        recorded history. The restriction exists because those files are also RCA's
        answer key, which is a fact about RCA and not about the corpus.
        """
        monkeypatch.setenv("AIOPS_INCIDENT_HISTORY_PROVIDERS", "mock")
        from aiops.tools.incident_history.retriever import _chain

        known, unknown = _chain()
        assert known == ["mock"]
        assert unknown == []


# ─── guard 2: the store emits only verified outcomes ────────────────────────


class TestStoreEmitsOnlyVerified:
    def test_the_provider_filters_by_status_regardless_of_the_caller(self):
        """Defence in depth: the agent also checks eligibility, but a guard that depends
        on every caller remembering to filter is not a guard."""
        from aiops.state.repository import save_rca_outcome
        from aiops.tools.incident_history import RetrievalQuery
        from aiops.tools.incident_history.providers.outcomes import RcaOutcomeHistoryProvider

        for status in ("new", "unverified", "superseded", "invalidated"):
            save_rca_outcome(
                incident_id=f"INC-{status}",
                affected_service="payment-service",
                predicted_root_cause="a guess nobody confirmed",
                selected_hypothesis_class="dependency_unavailable",
                memory_status=status,
                signatures=["PaymentRedisDown"],
            )

        result = RcaOutcomeHistoryProvider().search(
            RetrievalQuery(service="payment-service", signatures=["PaymentRedisDown"])
        )
        assert result.matches == []
        assert result.corpus_size == 0, "unverified rows must not even be counted as corpus"

    def test_an_empty_store_is_healthy_not_broken(self):
        """A cold start is a valid state. Reporting it unhealthy would make a new
        deployment indistinguishable from a broken one."""
        from aiops.tools.incident_history.providers.outcomes import RcaOutcomeHistoryProvider

        healthy, detail = RcaOutcomeHistoryProvider().health()
        assert healthy
        assert "0 recallable" in detail

    def test_a_recalled_prior_is_always_in_a_usable_status(self):
        from aiops.state.repository import save_rca_outcome

        save_rca_outcome(
            incident_id="INC-verified",
            affected_service="payment-service",
            predicted_root_cause="redis unreachable",
            selected_hypothesis_class="dependency_unavailable",
            memory_status=MemoryStatus.VERIFIED.value,
            verification_result="resolved",
            signatures=["PaymentRedisDown", "redis_up"],
        )
        result = memory.recall(
            service="payment-service", signatures=["PaymentRedisDown", "redis_up"], now=NOW
        )
        assert result.priors
        assert all(p.status.usable_for_ranking for p in result.priors)
        assert all(p.eligible for p in result.priors)


# ─── guard 3: no truth-derived cause can become a prior ─────────────────────


class TestNoTruthCauseReachesRca:
    def test_the_truth_corpus_is_still_the_leak_it_is_documented_to_be(self):
        """A positive control on the *premise*.

        If this ever fails, ``corpus.py`` stopped exposing truth-file causes and the
        allowlist may be reconsidered — but until then the guards below are load-bearing,
        and a guard whose premise is unverified tends to be quietly deleted as redundant.
        """
        from aiops.tools.incident_history.corpus import load_corpus, reset_corpus_for_tests

        reset_corpus_for_tests()
        corpus = load_corpus()
        recorded = {str(rec.get("recorded_cause") or "") for rec in corpus}
        causes = _truth_causes()
        assert causes, "expected ecommerce truth files to exist"
        leaked = [c for c in causes if c in recorded]
        assert leaked, (
            "premise no longer holds: the shared corpus no longer carries truth-file "
            "root_cause values, so re-derive whether the RCA allowlist is still needed"
        )

    def test_a_cold_recall_returns_no_truth_cause(self):
        """The end-to-end property: with the store empty, no prior exists at all — so no
        truth-file cause can reach the agent however the corpus is configured."""
        result = memory.recall(
            service="payment-service",
            signatures=["EcommercePaymentRedisDown", "payment_redis_up"],
            now=NOW,
        )
        assert result.priors == ()

    def test_no_prior_ever_carries_a_truth_file_cause_even_with_the_corpus_configured(
        self, monkeypatch
    ):
        """Belt and braces: configure the *platform* chain to the corpus tier, then recall.

        The allowlist means the corpus is never consulted for priors, so the agent sees
        nothing regardless of what the platform chain is set to.
        """
        monkeypatch.setenv("AIOPS_INCIDENT_HISTORY_PROVIDERS", "mock,embedding")
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "rca_outcomes")
        causes = _truth_causes()
        for path in _truth_files():
            data = json.loads(path.read_text(encoding="utf-8"))
            signals = data.get("expected_signals") or {}
            names = [
                str(entry.get("name"))
                for group in ("metrics", "container", "logs")
                for entry in signals.get(group) or []
                if isinstance(entry, dict) and entry.get("name")
            ]
            result = memory.recall(
                service=str(data.get("service") or "unknown"), signatures=names, now=NOW
            )
            for prior in result.priors:
                assert prior.recorded_cause not in causes, (
                    f"{path.stem}: a truth-file root_cause reached RCA as a prior"
                )

    def test_recorded_cause_is_the_field_name_and_root_cause_is_not(self):
        """``HistoricalPrior`` has no field that can express a claim about the *current*
        incident. Naming discipline, mirrored from ``ResolutionMetadata``."""
        from agents.rca_agent.investigation.models import HistoricalPrior

        fields = set(HistoricalPrior.model_fields)
        assert "recorded_cause" in fields
        assert "root_cause" not in fields
        assert "fault_category" not in fields


# ─── guard 3b: recall queries carry symptoms, not causes ────────────────────


class TestRecallQueriesAreSymptomOnly:
    def test_signatures_are_built_from_symptoms_not_from_the_cause(self):
        from agents.rca_agent.agent import _memory_signatures
        from agents.rca_agent.investigation.facts import (
            DependencyGauge,
            ErrorRate,
            FiringAlert,
            ObservedFacts,
            PodLifecycle,
        )

        facts = ObservedFacts(
            gauges=[DependencyGauge(metric="redis_up", label="cache", value=0.0)],
            error_rates=[
                ErrorRate(metric="payment_failures_total", reason="redis_error", rate=3.0)
            ],
            lifecycles=[PodLifecycle(pod="payment-1", terminated_reason="OOMKilled")],
            alerts=[FiringAlert(name="EcommercePaymentRedisDown")],
        )
        signatures = _memory_signatures(
            {"alert_summary": "EcommercePaymentRedisDown firing: cache down"}, facts, {}
        )

        assert "EcommercePaymentRedisDown" in signatures
        assert "redis_up" in signatures
        assert "payment_failures_total:redis_error" in signatures
        assert "terminated:OOMKilled" in signatures

    def test_no_hypothesis_name_enters_the_query(self):
        """Matching on a proposed cause retrieves the priors that agree with the
        conclusion already reached, and calls that corroboration. Only symptom matching
        is retrieval."""
        from agents.rca_agent.agent import _memory_signatures
        from agents.rca_agent.investigation.catalog import RULES
        from agents.rca_agent.investigation.facts import DependencyGauge, ObservedFacts

        facts = ObservedFacts(gauges=[DependencyGauge(metric="redis_up", label="cache", value=0.0)])
        signatures = [
            s.lower() for s in _memory_signatures({"alert_summary": "X firing: y"}, facts, {})
        ]
        for rule in RULES:
            assert rule.rule_id not in signatures
            assert rule.category not in signatures
            assert rule.action_category not in signatures

    def test_log_lines_are_excluded_so_they_cannot_dilute_a_match(self):
        from agents.rca_agent.agent import _memory_signatures
        from agents.rca_agent.investigation.facts import ObservedFacts

        facts = ObservedFacts(
            log_lines=["2026-08-12T10:00:00Z req=abc123 ERROR redis connection refused"]
        )
        signatures = _memory_signatures({}, facts, {})
        assert not any("req=abc123" in s for s in signatures)

    def test_a_healthy_dependency_contributes_no_unreachable_marker(self):
        """Otherwise two unrelated incidents on a service with healthy dependencies would
        look alike purely because both dependencies were fine."""
        from agents.rca_agent.agent import _memory_signatures
        from agents.rca_agent.investigation.facts import DependencyGauge, ObservedFacts

        facts = ObservedFacts(gauges=[DependencyGauge(metric="redis_up", label="cache", value=1.0)])
        signatures = _memory_signatures({}, facts, {})
        assert "redis_up" in signatures
        assert not any("unreachable" in s for s in signatures)


# ─── the agent path stays cold when it should ──────────────────────────────


class TestAgentPathIsolation:
    def test_offline_analysis_performs_no_recall(self, monkeypatch):
        """``run()`` is zero-I/O and must stay so — a recall is a database read."""
        called: list[str] = []
        monkeypatch.setattr(
            memory, "recall", lambda **kw: called.append("recalled") or memory.MemoryRecall()
        )
        from agents.rca_agent.agent import run

        run(
            {
                "triage_verdict": {
                    "affected_service": "payment-service",
                    "alert_summary": "X firing: y",
                    "severity": "sev2",
                }
            }
        )
        assert called == []

    def test_a_verdict_reports_its_memory_status_in_the_decision_trace(self, monkeypatch):
        """Cold start is stated, not silent: an operator should not have to guess whether
        memory was consulted."""
        monkeypatch.setenv("AIOPS_RCA_MEMORY_PROVIDERS", "")
        from agents.rca_agent.agent import analyze

        verdict = analyze(
            {
                "affected_service": "payment-service",
                "alert_summary": "EcommercePaymentRedisDown firing: cache down",
                "severity": "sev2",
            }
        )
        trace = " ".join(verdict.audit_metadata.decision_trace)
        assert "memory" in trace.lower()

    def test_an_outcome_is_not_recorded_merely_by_analysing(self):
        """Phase 3 reads memory; nothing on the analysis path writes it. Recording
        happens after verification, which is Phase 6 — so an analysis must leave the
        store exactly as it found it."""
        from aiops.state.repository import count_rca_outcomes

        before = count_rca_outcomes()
        from agents.rca_agent.agent import analyze

        analyze(
            {
                "affected_service": "payment-service",
                "alert_summary": "EcommercePaymentRedisDown firing: cache down",
                "severity": "sev2",
            }
        )
        assert count_rca_outcomes() == before


# ─── how memory may appear in the prompt (relaxed in Phase 4) ──────────────
#
# Phase 3 asserted that priors reached the prompt in NO form: the phase was retrieval
# and scoring only, and the flat prohibition was the cheapest way to keep the change
# from leaking into the model's context before it had been evaluated.
#
# Phase 4 renders the investigation into the prompt, and history is part of that
# investigation — §27 requires an operator to be *told* when a past incident moved a
# conclusion, and the sentence they read is written by the model. So the prohibition is
# replaced rather than deleted, and what it now pins is the shape:
#
#   * the system prompt still names no memory concept — a standing instruction about
#     priors would apply on every incident, including the ones where nothing was
#     recalled;
#   * memory appears in the *user* message only when it actually contributed;
#   * it is labelled as precedent, never as evidence from the incident at hand;
#   * no memory id or other internal identifier reaches the model.


class TestHowMemoryMayAppearInThePrompt:
    def test_the_system_prompt_names_no_memory_concept(self):
        from agents.rca_agent.agent import SYSTEM_PROMPT_V7

        lowered = SYSTEM_PROMPT_V7.lower()
        for term in ("historical prior", "recorded_cause", "memory_status", "prior_max"):
            assert term not in lowered

    def test_the_user_prompt_is_silent_about_memory_when_nothing_was_recalled(self):
        from agents.rca_agent.agent import _render_user_prompt

        prompt = _render_user_prompt(
            {"affected_service": "payment-service", "alert_summary": "X firing: y"},
            None,
            None,
            {"redis_up": ["0"]},
        )
        assert "prior" not in prompt.lower()
        assert "historical influence" not in prompt.lower()

    def test_when_memory_contributed_it_is_labelled_as_precedent(self):
        from agents.rca_agent.agent import _render_investigation_block
        from agents.rca_agent.investigation.models import HistoricalInfluence
        from tests.test_rca_prompt_v7 import TestInvestigationBlock

        block = _render_investigation_block(
            TestInvestigationBlock()._investigation(
                historical_influence=HistoricalInfluence(
                    level="moderate", priors_applied=("INC-42",)
                )
            )
        )
        assert "precedent" in block.lower()
        assert "never as evidence from this incident" in block

    def test_no_memory_id_reaches_the_model(self):
        """An id in a prompt comes back out inside the prose an engineer reads."""
        from agents.rca_agent.agent import _render_investigation_block
        from agents.rca_agent.investigation.models import HistoricalInfluence
        from tests.test_rca_prompt_v7 import TestInvestigationBlock

        block = _render_investigation_block(
            TestInvestigationBlock()._investigation(
                historical_influence=HistoricalInfluence(
                    level="moderate", priors_applied=("INC-42", "INC-43")
                )
            )
        )
        assert "INC-42" not in block
        assert "INC-43" not in block
        # The count is what the operator needs, not the identifiers.
        assert "2 verified prior(s)" in block


# ─── the memory record itself keeps the prediction auditable ────────────────


class TestOutcomeRecordAudit:
    def test_a_prediction_and_its_correction_are_both_kept(self):
        """Overwriting the prediction with the truth destroys the only data that shows the
        agent was wrong, which is exactly what calibration needs."""
        from agents.rca_agent.investigation.models import RCAOutcome

        outcome = RCAOutcome(
            incident_id="INC-1",
            affected_service="payment-service",
            recorded_at=NOW,
            predicted_root_cause="dns failure",
            predicted_status=RootCauseStatus.PROBABLE,
            confidence=0.7,
            verification_result="not_resolved",
            human_corrected_root_cause="redis unreachable",
        )
        row_id = memory.record_outcome(outcome)
        assert row_id is not None

        from aiops.state.repository import get_rca_outcome

        row = get_rca_outcome(row_id)
        assert row is not None
        assert row["predicted_root_cause"] == "dns failure"
        assert row["human_corrected_root_cause"] == "redis unreachable"
        assert row["outcome"]["confidence"] == 0.7
