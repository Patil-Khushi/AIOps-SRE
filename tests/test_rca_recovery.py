"""Phase 5 — blast radius, recovery planning, risk, verification, and action grounding.

The four claims this phase makes, and where each could silently stop being true:

1. **Grounding works offline.** Before Phase 5 it asked a provider that only exists in the
   demo layer and returned the steps *unchanged* when it could not be reached — so in CI,
   in every eval run, and on any laptop without the cluster, an invented action key passed
   straight through. It now shares one authority with the prompt vocabulary.
2. **"Never checked" is not "healthy".** A blast radius that reports an unqueried service
   as fine is worse than no blast radius, because it reads as an all-clear.
3. **A failure class becomes a runnable action only when the tokens actually decide.**
   Ambiguity produces a manual step, not a guess with a button on it.
4. **Risk is tri-state, and the unanswered questions are counted.** A register full of
   ``False`` that nobody evaluated is how a dangerous action gets approved.
"""

from __future__ import annotations

import pytest

from agents.rca_agent.investigation import impact, recovery
from agents.rca_agent.investigation.facts import (
    DependencyGauge,
    ErrorRate,
    FiringAlert,
    LatencyP95,
    ObservedFacts,
)
from agents.rca_agent.investigation.models import (
    EvidenceItem,
    EvidenceMatrix,
    EvidenceStance,
    Hypothesis,
    HypothesisScore,
    ImpactState,
    IncidentScope,
)
from agents.rca_agent.models import BlastRadius, FixActionType, RankedFixStep

PAYMENT_VOCAB = (
    "payment_service.redis_down",
    "payment_service.http_500",
    "payment_service.high_cpu",
    "payment_service.gateway_timeout",
)


def _scope(service: str = "payment-service", *, topology: tuple[str, ...] = ()) -> IncidentScope:
    return IncidentScope(
        incident_id="INC-1",
        affected_service=service,
        severity="sev2",
        user_visible_symptom="requests are failing with errors",
        initial_blast_radius=topology,
    )


def _hypothesis(category: str, component: str | None = None, hint: str = "restore_dependency"):
    return Hypothesis(
        hypothesis_id=f"h-{category}",
        label=category.replace("_", " "),
        mechanism=f"{category} on {component or 'the service'}",
        candidate_component=component,
        category=category,
        action_hint=hint,
    )


def _matrix(category: str, component: str | None = None, *, score: float = 0.8, supporting=1):
    return EvidenceMatrix(
        hypothesis=_hypothesis(category, component),
        supporting=tuple(
            EvidenceItem(
                evidence_id=f"e{i}",
                stance=EvidenceStance.SUPPORTS,
                statement=f"observation {i} about {component or category}",
            )
            for i in range(supporting)
        ),
        score=HypothesisScore(score=score),
    )


# ─── 1. grounding works offline ─────────────────────────────────────────────


class TestGroundingOffline:
    def _step(self, flag: str) -> RankedFixStep:
        return RankedFixStep(
            description="Clear it",
            blast_radius=BlastRadius.LOW,
            rollback="undo",
            action_type=FixActionType.SET_FLAG,
            flag=flag,
        )

    def test_a_real_key_survives(self, monkeypatch):
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: None)
        trace: list[str] = []
        out = agent._ensure_executable_action(
            [self._step("payment_service.redis_down")],
            service="payment-service",
            decision_trace=trace,
        )
        assert out[0].action_type is FixActionType.SET_FLAG
        assert out[0].flag == "payment_service.redis_down"

    def test_an_invented_key_is_downgraded_with_no_registry(self, monkeypatch):
        """The regression this phase fixes.

        The old grounding asked ``_live_flag_names()`` and returned the steps *unchanged*
        when it answered ``None`` — which it always does without the demo layer. So the
        one path where nothing else validates the key was the path that skipped
        validation.
        """
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: None)
        trace: list[str] = []
        out = agent._ensure_executable_action(
            [self._step("payment_service.totally_invented")],
            service="payment-service",
            decision_trace=trace,
        )
        assert out[0].action_type is FixActionType.MANUAL
        assert out[0].flag is None
        assert any("not an action the platform can execute" in line for line in trace)

    def test_a_cross_service_key_is_downgraded(self, monkeypatch):
        """Rejected even when the key is perfectly real.

        The old check compared against the *global* key set, so a real key belonging to
        another service passed — online as well as offline. An action that runs and fixes
        a different service's problem is worse than one that fails.
        """
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: None)
        trace: list[str] = []
        out = agent._ensure_executable_action(
            [self._step("order_service.http_500")],
            service="payment-service",
            decision_trace=trace,
        )
        assert out[0].action_type is FixActionType.MANUAL

    def test_an_authoritative_empty_vocabulary_still_downgrades(self, monkeypatch):
        """The regression the full suite caught, and the distinction behind it.

        "The registry answered and this service has no action" is an *authoritative* empty
        answer — an invented key must be downgraded. "Nobody could tell us what is
        runnable" is ignorance, and only then is failing open correct. The first version of
        this check treated both as "empty vocabulary → skip grounding", so
        ``frontendFailure`` on an unmapped service survived as a clickable button.
        """
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: {"payment_service.redis_down"})
        trace: list[str] = []
        out = agent._ensure_executable_action(
            [self._step("frontendFailure")], service="frontend", decision_trace=trace
        )
        assert out[0].action_type is FixActionType.MANUAL
        assert any("no executable action for 'frontend'" in line for line in trace)

    def test_a_dotless_key_the_registry_lists_is_kept(self, monkeypatch):
        """Legacy handles carry no service, so a mismatch cannot be proven.

        ``order_service.http_500`` on payment-service is provably wrong — the service is in
        the key. ``emailMemoryLeak`` names no service at all, so if the registry lists it,
        rejecting it would be inventing a fault.
        """
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: {"emailMemoryLeak"})
        out = agent._ensure_executable_action(
            [self._step("emailMemoryLeak")], service="emailservice", decision_trace=[]
        )
        assert out[0].action_type is FixActionType.SET_FLAG

    def test_a_dotless_key_the_registry_does_not_list_is_downgraded(self, monkeypatch):
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: {"emailMemoryLeak"})
        out = agent._ensure_executable_action(
            [self._step("inventedHandle")], service="emailservice", decision_trace=[]
        )
        assert out[0].action_type is FixActionType.MANUAL

    def test_it_still_fails_open_when_nothing_knows_the_service(self, monkeypatch):
        """One remaining open failure, and it is the right one: the platform genuinely
        cannot say what is runnable, so the executor is the only check left — and that is
        recorded rather than silent."""
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: None)
        trace: list[str] = []
        out = agent._ensure_executable_action(
            [self._step("whatever.key")],
            service="a-service-nobody-registered",
            decision_trace=trace,
        )
        assert out[0].action_type is FixActionType.SET_FLAG
        assert any("action grounding skipped" in line for line in trace)

    def test_manual_steps_are_untouched(self, monkeypatch):
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: None)
        manual = RankedFixStep(
            description="Look at it",
            blast_radius=BlastRadius.LOW,
            rollback="N/A",
            action_type=FixActionType.MANUAL,
        )
        out = agent._ensure_executable_action(
            [manual], service="payment-service", decision_trace=[]
        )
        assert out == [manual]

    def test_grounding_and_the_prompt_share_one_authority(self, monkeypatch):
        """The invariant that keeps them from drifting: the list the model is *offered* and
        the list it is *held to* come from the same function."""
        from agents.rca_agent import agent

        monkeypatch.setattr(agent, "_live_flag_names", lambda: None)
        offered, _ = agent._action_vocabulary("payment-service")
        block = agent._render_action_block("payment-service")
        for key in offered:
            assert key in block
        for key in offered:
            out = agent._ensure_executable_action(
                [self._step(key)], service="payment-service", decision_trace=[]
            )
            assert out[0].action_type is FixActionType.SET_FLAG, key


class TestExecutorAvailability:
    def test_only_the_registry_implies_an_executor(self):
        from agents.rca_agent.agent import (
            VOCAB_FROM_FALLBACK,
            VOCAB_FROM_REGISTRY,
            VOCAB_UNAVAILABLE,
            executor_available,
        )

        assert executor_available(VOCAB_FROM_REGISTRY) is True
        assert executor_available(VOCAB_FROM_FALLBACK) is False
        assert executor_available(VOCAB_UNAVAILABLE) is False

    def test_the_check_is_not_a_substring_match(self):
        """The bug this pins: the first version asked ``"registry" in source``, and the
        *fallback* string says "the action registry was unreachable". So ``executable``
        came back True offline and the grounded/executable split collapsed — a negation
        read as a confirmation."""
        from agents.rca_agent.agent import VOCAB_FROM_FALLBACK, executor_available

        assert "registry" in VOCAB_FROM_FALLBACK
        assert executor_available(VOCAB_FROM_FALLBACK) is False


# ─── 2. never checked is not healthy ────────────────────────────────────────


class TestBlastRadius:
    def test_the_alerting_service_is_directly_affected_when_anything_was_observed(self):
        facts = ObservedFacts(alerts=[FiringAlert(name="X")])
        report = impact.build_blast_radius(_scope(), facts)
        assert report.impacts[0].service == "payment-service"
        assert report.impacts[0].state is ImpactState.DIRECTLY_AFFECTED
        assert report.impacts[0].hops == 0

    def test_with_no_telemetry_the_service_is_unknown_not_healthy(self):
        """An alert fired. Reporting the service healthy because nothing was collected
        would contradict the only fact available."""
        report = impact.build_blast_radius(_scope(), ObservedFacts())
        assert report.impacts[0].state is ImpactState.UNKNOWN
        assert "unestablished rather than absent" in report.impacts[0].rationale

    def test_an_unreachable_store_is_directly_affected(self):
        facts = ObservedFacts(
            gauges=[DependencyGauge(metric="redis_up", label="Redis (payment-service)", value=0.0)]
        )
        report = impact.build_blast_radius(_scope(), facts)
        redis = next(i for i in report.impacts if i.service == "Redis")
        assert redis.state is ImpactState.DIRECTLY_AFFECTED
        assert redis.relation == "dependency"
        assert redis.hops == 1

    def test_a_reachable_store_is_observed_healthy(self):
        facts = ObservedFacts(
            gauges=[DependencyGauge(metric="redis_up", label="Redis (payment-service)", value=1.0)]
        )
        report = impact.build_blast_radius(_scope(), facts)
        redis = next(i for i in report.impacts if i.service == "Redis")
        assert redis.state is ImpactState.OBSERVED_HEALTHY

    def test_a_topology_neighbour_nobody_queried_is_not_observed(self):
        """The distinction the whole module exists for. An unexamined dependent is where
        undetected user impact hides, and omitting it reads as "nothing else is affected"."""
        facts = ObservedFacts(alerts=[FiringAlert(name="X")])
        report = impact.build_blast_radius(_scope(topology=("frontend", "order-service")), facts)
        states = {i.service: i.state for i in report.impacts}
        assert states["frontend"] is ImpactState.NOT_OBSERVED
        assert states["order-service"] is ImpactState.NOT_OBSERVED
        for service in ("frontend", "order-service"):
            rationale = next(i.rationale for i in report.impacts if i.service == service)
            assert "not ruled out" in rationale

    def test_no_topology_is_reported_as_a_blind_spot(self):
        report = impact.build_blast_radius(_scope(), ObservedFacts(alerts=[FiringAlert(name="X")]))
        assert report.topology_available is False
        assert report.note is not None
        assert "neither listed nor ruled out" in report.note

    def test_topology_present_clears_the_note(self):
        report = impact.build_blast_radius(
            _scope(topology=("frontend",)), ObservedFacts(alerts=[FiringAlert(name="X")])
        )
        assert report.topology_available is True
        assert report.note is None

    def test_a_store_is_not_listed_twice(self):
        """Gauge labels carry the owning service in parentheses; the same store rendered
        two ways would appear as two impacts."""
        facts = ObservedFacts(
            gauges=[
                DependencyGauge(metric="redis_up", label="Redis (payment-service)", value=0.0),
                DependencyGauge(metric="redis_up", label="Redis", value=0.0),
            ]
        )
        report = impact.build_blast_radius(_scope(), facts)
        assert [i.service for i in report.impacts].count("Redis") == 1

    def test_endpoints_come_only_from_measured_signals(self):
        facts = ObservedFacts(
            latencies=[
                LatencyP95(hop="/payments", seconds=3.0, threshold=1.0),
                LatencyP95(hop="/healthz", seconds=0.1, threshold=1.0),
            ],
            error_rates=[
                ErrorRate(metric="payment_failures_total", reason="redis_error", rate=2.0),
                ErrorRate(metric="quiet_total", reason="none", rate=0.0),
            ],
        )
        report = impact.build_blast_radius(_scope(), facts)
        assert "/payments" in report.affected_endpoints
        assert "/healthz" not in report.affected_endpoints
        assert "payment_failures_total" in report.affected_endpoints
        assert "quiet_total" not in report.affected_endpoints

    def test_the_report_carries_no_incident_level_enum(self):
        """Deliberate. ``RankedFixStep.blast_radius`` is *action risk*; this report is
        *incident spread*. Two similarly-named numbers meaning different things is worse
        than one structured report, so the enum summary was dropped."""
        assert not hasattr(impact, "derive_blast_radius")


# ─── 3. a class becomes an action only when the tokens decide ───────────────


class TestActionKeyMatching:
    def test_a_clear_match_is_found_and_explained(self):
        key, why = recovery.match_action_key(
            _hypothesis("dependency_unavailable", "Redis (payment-service)"), PAYMENT_VOCAB
        )
        assert key == "payment_service.redis_down"
        assert "redis" in why

    def test_cpu_saturation_finds_the_cpu_action(self):
        key, _ = recovery.match_action_key(
            _hypothesis("resource_saturation_cpu", "payment-service-abc"), PAYMENT_VOCAB
        )
        assert key == "payment_service.high_cpu"

    def test_an_ambiguous_match_produces_no_action(self):
        """Two keys tied on the same tokens means the tokens did not choose. Taking the
        alphabetically-first would be a guess presented as a grounded action."""
        key, why = recovery.match_action_key(
            _hypothesis("timeout", "gateway"),
            ("payment_service.gateway_timeout", "order_service.gateway_timeout"),
        )
        assert key is None
        assert "did not discriminate" in why

    def test_no_shared_token_produces_no_action(self):
        key, why = recovery.match_action_key(
            _hypothesis("change_induced_regression", "a recent commit"), PAYMENT_VOCAB
        )
        assert key is None
        assert "no action key shares" in why

    def test_an_empty_vocabulary_produces_no_action(self):
        key, why = recovery.match_action_key(_hypothesis("dependency_unavailable", "Redis"), ())
        assert key is None
        assert "no executable action vocabulary" in why

    def test_generic_words_alone_never_match(self):
        """Without stopword removal, "service" matches every key for every service and the
        first one always wins — which looks like grounding and is a coin flip."""
        key, _ = recovery.match_action_key(_hypothesis("unknown", "the service"), PAYMENT_VOCAB)
        assert key is None

    def test_matching_is_reproducible(self):
        hypothesis = _hypothesis("dependency_unavailable", "Redis (payment-service)")
        first = recovery.match_action_key(hypothesis, PAYMENT_VOCAB)
        second = recovery.match_action_key(hypothesis, tuple(reversed(PAYMENT_VOCAB)))
        assert first[0] == second[0]


class TestRecoveryOptions:
    def test_a_grounded_option_is_offered_for_the_top_hypothesis(self):
        options = recovery.build_recovery_options(
            (_matrix("dependency_unavailable", "Redis (payment-service)"),),
            vocabulary=PAYMENT_VOCAB,
            executor_available=True,
        )
        assert options[0].action_key == "payment_service.redis_down"
        assert options[0].grounded is True
        assert options[0].executable is True

    def test_grounded_and_executable_come_apart_offline(self):
        """The distinction that lets the dashboard say "known fix, no executor here"
        instead of offering a button that fails after approval."""
        options = recovery.build_recovery_options(
            (_matrix("dependency_unavailable", "Redis (payment-service)"),),
            vocabulary=PAYMENT_VOCAB,
            executor_available=False,
        )
        assert options[0].grounded is True
        assert options[0].executable is False

    def test_an_unmatched_hypothesis_yields_a_manual_option(self):
        options = recovery.build_recovery_options(
            (_matrix("change_induced_regression", "a recent commit"),),
            vocabulary=PAYMENT_VOCAB,
            executor_available=True,
        )
        assert options[0].action_key is None
        assert options[0].grounded is False
        assert options[0].executable is False
        assert "manually" in options[0].description

    def test_the_same_action_is_never_offered_twice(self):
        """Two hypotheses can legitimately match one key — "the store is unreachable" and
        "requests fail with a store error" are two readings of one failure. Offering it
        twice puts two identical approve buttons on the screen."""
        options = recovery.build_recovery_options(
            (
                _matrix("dependency_unavailable", "Redis (payment-service)", score=0.82),
                _matrix("application_error", "Redis (payment-service)", score=0.47),
            ),
            vocabulary=PAYMENT_VOCAB,
            executor_available=True,
        )
        keys = [o.action_key for o in options if o.action_key]
        assert len(keys) == len(set(keys))
        assert options[1].action_key is None
        assert "already proposed for the higher-ranked" in options[1].why_it_addresses_the_cause

    def test_every_option_requires_hitl_by_type(self):
        """``requires_hitl`` is ``Literal[True]``, so an option that bypasses approval
        cannot be constructed rather than being caught by a later check."""
        options = recovery.build_recovery_options(
            (_matrix("dependency_unavailable", "Redis (payment-service)"),),
            vocabulary=PAYMENT_VOCAB,
            executor_available=True,
        )
        assert all(o.requires_hitl is True for o in options)
        with pytest.raises(Exception):
            options[0].model_copy(update={"requires_hitl": False}).model_validate(
                options[0].model_dump() | {"requires_hitl": False}
            )

    def test_options_are_bounded(self):
        matrices = tuple(_matrix(f"class_{i}", f"component-{i}", score=0.5) for i in range(6))
        options = recovery.build_recovery_options(
            matrices, vocabulary=PAYMENT_VOCAB, executor_available=True, limit=2
        )
        assert len(options) == 2

    def test_the_option_says_why_it_addresses_the_cause(self):
        options = recovery.build_recovery_options(
            (_matrix("dependency_unavailable", "Redis (payment-service)", score=0.82),),
            vocabulary=PAYMENT_VOCAB,
            executor_available=True,
        )
        why = options[0].why_it_addresses_the_cause
        assert "dependency_unavailable" in why
        assert "0.82" in why


# ─── 4. risk is tri-state and the gaps are counted ─────────────────────────


class TestRiskAssessment:
    def test_unanswered_questions_are_none_not_false(self):
        risk = recovery.assess_risk(
            action_key="payment_service.redis_down",
            hypothesis=_hypothesis("dependency_unavailable", "Redis"),
        )
        assert risk.risks_data_loss is None
        assert risk.risks_duplicate_transactions is None
        assert risk.unassessed, "the gaps must be counted, not silently defaulted"

    def test_a_restarting_action_interrupts_requests_and_reads_medium(self):
        risk = recovery.assess_risk(
            action_key="user_service.crashloop", hypothesis=_hypothesis("process_crash_loop")
        )
        assert risk.interrupts_active_requests is True
        assert risk.destroys_evidence is True
        assert risk.level == "medium"
        assert any("in-flight requests" in c for c in risk.concerns)

    def test_a_datastore_action_affects_downstream(self):
        risk = recovery.assess_risk(
            action_key="payment_service.redis_down",
            hypothesis=_hypothesis("dependency_unavailable", "Redis"),
        )
        assert risk.affects_downstream is True
        assert any("every caller" in c for c in risk.concerns)

    def test_never_high_from_this_evidence(self):
        """HIGH should mean data loss or irreversibility, and nothing available here
        establishes either. Claiming it would be alarm from a blind spot."""
        for key in (
            "payment_service.redis_down",
            "user_service.crashloop",
            "order_service.memory_leak_oom",
            "payment_service.high_cpu",
        ):
            risk = recovery.assess_risk(action_key=key, hypothesis=_hypothesis("x"))
            assert risk.level != "high", key

    def test_a_manual_step_is_reversible_with_nothing_to_roll_back(self):
        """``reversible`` is a strict bool while the risk questions are tri-state, and the
        asymmetry is deliberate — reversibility is a requirement the platform must state.
        A step that changes nothing is trivially reversible."""
        risk = recovery.assess_risk(action_key=None, hypothesis=_hypothesis("x"))
        assert risk.level == "unknown"
        assert risk.reversible is True
        assert risk.rollback_available is False

    def test_the_rationale_says_risk_came_from_the_action_not_the_diagnosis(self):
        risk = recovery.assess_risk(
            action_key="payment_service.redis_down", hypothesis=_hypothesis("x")
        )
        assert "from the action" in risk.rationale
        assert "not as safe" in risk.rationale


class TestVerificationPlan:
    def test_the_checks_are_the_supporting_evidence_read_back(self):
        """Inventing separate criteria would let a fix pass verification without touching
        the signal that raised the incident."""
        matrix = _matrix("dependency_unavailable", "Redis", supporting=3)
        plan = recovery.build_verification_plan((matrix,))
        assert len(plan.checks) == 3
        assert all("observation" in c for c in plan.checks)

    def test_the_offsets_match_the_resolution_verifier(self):
        """RCA writes the plan and ``resolution_verifier`` executes it. A second cadence
        would mean they disagreed about when "not resolved" is established."""
        plan = recovery.build_verification_plan((_matrix("x", "y"),))
        assert plan.window_seconds == recovery.DEFAULT_RECHECK_OFFSETS == (60, 180, 300)

    def test_a_partial_recovery_is_not_a_pass(self):
        plan = recovery.build_verification_plan((_matrix("x", "y"),))
        assert any("NOT resolved" in c for c in plan.success_criteria)

    def test_failure_routes_back_to_investigation_not_a_retry(self):
        """A fix that did not work is evidence the cause was wrong, so retrying it is the
        one thing the plan must not say."""
        plan = recovery.build_verification_plan((_matrix("dependency_unavailable", "Redis"),))
        assert "re-investigate" in plan.if_not_resolved
        assert "retry" not in plan.if_not_resolved.lower()

    def test_no_cause_means_nothing_to_verify(self):
        plan = recovery.build_verification_plan(())
        assert plan.checks == ()
        assert "nothing to verify" in plan.success_criteria[0]


# ─── the stages as wired into the pipeline ─────────────────────────────────


class TestPipelineWiring:
    def _facts(self):
        return ObservedFacts(
            gauges=[
                DependencyGauge(metric="redis_up", label="Redis (payment-service)", value=0.0),
                DependencyGauge(metric="mysql_up", label="MySQL (user-service)", value=1.0),
            ],
            alerts=[FiringAlert(name="EcommerceRedisDown")],
        )

    def test_investigate_populates_all_three_stages(self):
        from agents.rca_agent.investigation import pipeline

        result = pipeline.investigate(
            {"affected_service": "payment-service", "severity": "sev2"},
            self._facts(),
            action_vocabulary=PAYMENT_VOCAB,
            executor_available=False,
        )
        assert result.blast_radius is not None
        assert result.recovery_options
        assert result.verification is not None

    def test_no_recovery_is_planned_for_an_unactionable_status(self):
        """``is_actionable`` is the single predicate for "offer a remediation". A button
        beside the word "uncertain" is the failure mode."""
        from agents.rca_agent.investigation import pipeline

        result = pipeline.investigate(
            {"affected_service": "payment-service", "severity": "sev2"},
            ObservedFacts(alerts=[FiringAlert(name="X")]),
            action_vocabulary=PAYMENT_VOCAB,
            executor_available=True,
        )
        if not result.status.is_actionable:
            assert result.recovery_options == ()

    def test_the_blast_radius_stage_runs_even_with_no_hypothesis(self):
        """Impact is a fact about the incident, not about the diagnosis — it should be
        reported whether or not a cause was found."""
        from agents.rca_agent.investigation import pipeline

        result = pipeline.investigate(
            {"affected_service": "payment-service", "severity": "sev2"},
            ObservedFacts(),
            action_vocabulary=PAYMENT_VOCAB,
        )
        assert result.blast_radius is not None
        assert result.blast_radius.impacts

    def test_the_no_llm_verdict_now_proposes_an_executable_step(self, monkeypatch):
        """What Phase 5 buys end to end. Before it, the deterministic path emitted a single
        manual step because grounding could not run offline, so ``remediation_accuracy``
        was 0.0 on the no-LLM arm by construction."""
        from agents.rca_agent import agent
        from agents.rca_agent.investigation import pipeline

        monkeypatch.setattr(agent, "_live_flag_names", lambda: None)
        investigation = pipeline.investigate(
            {"affected_service": "payment-service", "severity": "sev2"},
            self._facts(),
            action_vocabulary=PAYMENT_VOCAB,
            executor_available=False,
        )
        verdict = agent._verdict_from_investigation(
            investigation, service="payment-service", decision_trace=[]
        )
        flags = [s.flag for s in verdict.ranked_fix_steps if s.flag]
        assert "payment_service.redis_down" in flags
        assert all(s.requires_hitl is True for s in verdict.ranked_fix_steps)
