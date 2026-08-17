"""The hypothesis catalog: generic failure classes, triggered by evidence.

Why the candidate space is failure *classes* and not failure *keys*
------------------------------------------------------------------
The obvious way to generate hypotheses here would be to enumerate the platform's
failure keys — ``user_service.mysql_down``, ``order_service.memory_leak_oom`` — and ask
which one fits. That is exactly what must not happen. Those keys are the
failure-injection vocabulary; hypothesising from them makes the RCA a multiple-choice
test over the answer sheet, it cannot generalise to an incident nobody injected, and it
puts injection truth into the reasoning path (constraint #9/#10).

So the candidate space is the generic vocabulary a production SRE carries between jobs:
a dependency is unreachable, a process is crash-looping, a container is CPU-bound, a
downstream call is timing out, a deploy preceded onset, the alert is stale. None of it
mentions this system's faults, and all of it is testable against telemetry any
Kubernetes service emits.

Each rule is **evidence-triggered**: it proposes itself only when something observed
supports it. A rule that fired unconditionally would produce ten hypotheses for every
incident and push the discrimination work onto the LLM, which is the design this
replaces.

Contradiction is a first-class output
-------------------------------------
Every rule returns what argues *against* it as well as what argues for it, because a
matrix whose ``contradicting`` list is empty because nobody looked is indistinguishable
from one where nothing contradicts — unless the search is part of the rule's contract.
``needs`` names the fact categories required to test the rule at all, so an untestable
rule reports a gap instead of a quiet absence.

Actions stay generic here
-------------------------
``action_category`` is a *class* of remediation ("restore the unreachable dependency"),
never an executable key. Grounding a category against the live action registry is a
separate, later step, and unknown actions must degrade to manual investigation. A rule
that named a runnable key would put the LLM's candidate space and the executor's
capability list in the same object, which is precisely the coupling action grounding
exists to break.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from agents.rca_agent.investigation.facts import ObservedFacts

# Fact categories a rule can depend on. Used to report gaps: a rule that needs pod
# lifecycle data on a build where Kubernetes metrics are unavailable is *untested*,
# not refuted.
NEED_GAUGES = "dependency_health"
NEED_ERRORS = "error_breakdown"
NEED_LATENCY = "latency"
NEED_RESOURCES = "resource_saturation"
NEED_LIFECYCLE = "pod_state"
NEED_CHANGES = "recent_changes"


@dataclass
class RuleOutcome:
    """One rule's reading of the evidence."""

    supporting: list[tuple[str, str]] = field(default_factory=list)
    """``(statement, source)``. Statements are quoted from observation so a claim can
    always be traced to the reading behind it."""

    contradicting: list[tuple[str, str]] = field(default_factory=list)
    component: str | None = None
    """What is believed to be *at fault* — routinely not the service that alerted."""

    @property
    def triggered(self) -> bool:
        """Whether this rule proposes itself at all."""
        return bool(self.supporting)


def _log_support(facts: ObservedFacts, *needles: str) -> list[tuple[str, str]]:
    """Recent log lines mentioning any of ``needles``, as supporting evidence.

    This is where cross-source corroboration comes from, and without it no rule could
    ever earn ``DELTA_CROSS_SOURCE``: every other signal in this catalog is a metric, so
    a matrix built from metrics alone always looks single-source no matter how strong it
    is. A log line naming the same failure as a gauge is the strongest inference the
    agent can draw — one backend can be its own instrumentation artefact, two agreeing
    cannot.

    Lines are truncated in the statement because the raw text can be long and the
    statement is rendered to an operator; the full line is still reachable through the
    evidence id.
    """
    lowered = tuple(n.lower() for n in needles)
    return [
        (f'log: "{line[:120]}"', "logs")
        for line in facts.log_lines
        if any(n in line.lower() for n in lowered)
    ]


@dataclass(frozen=True)
class HypothesisRule:
    rule_id: str
    label: str
    category: str
    mechanism: str
    action_category: str
    needs: tuple[str, ...]
    evaluate: Callable[[ObservedFacts], RuleOutcome]


# ─── rules ──────────────────────────────────────────────────────────────────


def _dependency_unavailable(facts: ObservedFacts) -> RuleOutcome:
    """A backing store the service reports it cannot reach — or that is confirmed
    down at the cluster level regardless of what the service can report about itself.

    Near-conclusive when present, and — just as usefully — a gauge reading REACHABLE
    positively rules that store out. Two independent evidence sources feed this:
    the service's own connection-attempt gauge (ambiguous when the service itself is
    crash-looping, since its exporter then can't be scraped at all — see the
    "crashing" contradiction below), and a direct cluster-level readiness check on
    the datastore's own StatefulSet, which stays decisive in exactly that case.
    """
    out = RuleOutcome()
    down = facts.unreachable_stores
    for gauge in down:
        out.supporting.append((f"{gauge.label}: UNREACHABLE (gauge={gauge.value:g})", "metrics"))
        out.component = out.component or gauge.label

    # Cluster-level datastore readiness (independent of the consuming service's own
    # gauge — see DatastoreReadiness's docstring). This is NOT subject to the
    # "crashing pod contradicts" downgrade below: it is read directly off the
    # datastore's own StatefulSet, so it stays decisive even when the affected
    # service is itself crash-looping and cannot report its own gauge at all —
    # exactly the case where the self-reported gauge goes silent instead of reading 0.
    unready = facts.unready_datastores
    for store in unready:
        out.supporting.append(
            (
                f"{store.label} StatefulSet '{store.statefulset}': 0 ready replicas "
                "(cluster-level check, independent of the affected service's own gauge)",
                "metrics",
            )
        )
        out.component = out.component or store.label

    healthy = [g.label for g in facts.gauges if g.reachable]
    if down and healthy:
        # A reachable store does NOT contradict this hypothesis — it *narrows* it. An
        # earlier version listed every REACHABLE gauge as contradicting evidence, which
        # scored the correct answer down on exactly the scenarios where the gauges were
        # most decisive: two healthy stores beside one dead one is the strongest possible
        # localisation, not a counter-argument.
        out.supporting.append(
            (f"{', '.join(healthy)} REACHABLE, narrowing the fault to {down[0].label}", "metrics")
        )
    elif facts.gauges:
        out.contradicting.append(
            (f"every dependency gauge reads REACHABLE ({', '.join(healthy)})", "metrics")
        )

    if down:
        out.supporting.extend(
            _log_support(facts, "connection", "database", "db ", "redis", "mysql", "postgres")
        )
        # A pod dying on startup can make its own dependency gauge read 0 — the process
        # never gets far enough to connect — so an unreachable store beside a crash-looping
        # container is as likely to be a *symptom* as a cause. Recording it as a
        # contradiction is what lets the startup hypothesis, which explains both the
        # restarts and the gauge, outrank this one instead of tying with it.
        crashing = [life.pod for life in facts.lifecycles if life.died_without_oom]
        if crashing:
            out.contradicting.append(
                (
                    f"pod {crashing[0]} is terminating abnormally, so the unreachable store "
                    "may be a consequence of the failed startup rather than its cause",
                    "metrics",
                )
            )
        for rate in facts.reason_rates("db", "database", "sql", "redis"):
            out.supporting.append(
                (f"{rate.metric} reason={rate.reason}: {rate.rate:.3f}/s", "metrics")
            )

    # An absent database-flavoured error counter is deliberately NOT recorded as
    # contradicting evidence, though an earlier version did. Error counters here are
    # per-service and incomplete — user-service publishes ``login_failure_total`` while
    # the order path publishes ``orders_failed_total`` — so "no db error counter is
    # moving" often means "this service has no such counter that we query", not "the
    # database is fine". It fired on ``user_service_mysql_down`` and cost the correct
    # answer 0.35, taking it from PROBABLE to UNCERTAIN. A gauge at 0 is the service's
    # own failed connection attempt; an unrelated counter's silence cannot outweigh it.
    return out


def _dependency_timeout(facts: ObservedFacts) -> RuleOutcome:
    """A downstream or external call is timing out rather than failing fast.

    Deliberately separate from an error-rate hypothesis: a timeout points at the callee
    (or the network between), whereas a 500 points at the service that raised it. The
    service that *reports* a timeout is usually the victim, and conflating the two is
    how a victim gets remediated instead of a cause.
    """
    out = RuleOutcome()
    timeouts = facts.reason_rates("timeout")
    if not timeouts:
        # A timeout hypothesis requires a timeout *signal*. An earlier version also fired
        # on any latency breach, which made it indistinguishable from
        # ``_latency_regression`` — both rules claimed the same p95, and on
        # ``user_service_high_latency`` the timeout rule outscored the correct latency one
        # using evidence that says nothing about timing out. Slow and timed-out are
        # different findings: one names a threshold crossing, the other names calls that
        # never returned.
        return out
    for rate in timeouts:
        out.supporting.append((f"{rate.metric} reason={rate.reason}: {rate.rate:.3f}/s", "metrics"))
    for latency in facts.latencies:
        if latency.breaches_threshold:
            out.supporting.append(
                (f"{latency.hop} p95 {latency.seconds:.2f}s exceeds its threshold", "metrics")
            )
    if out.supporting:
        out.supporting.extend(_log_support(facts, "timeout", "timed out", "deadline"))
        if facts.gauges and not facts.unreachable_stores:
            # Healthy gauges SUPPORT a timeout hypothesis — they rule out the rival
            # explanation that a store is simply down. An earlier version recorded this
            # as contradicting, which penalised the correct answer on
            # ``payment_service_gateway_timeout`` by 0.35: the same inversion as the one
            # fixed in ``_dependency_unavailable``, and worth stating twice because it is
            # the easiest mistake to make in this file. "Rules out the alternative" and
            # "argues against this" are opposites.
            out.supporting.append(
                (
                    "every dependency gauge reads REACHABLE, so this is a slow call rather "
                    "than a store outage",
                    "metrics",
                )
            )
    return out


def _resource_saturation_cpu(facts: ObservedFacts) -> RuleOutcome:
    """A container at or near its CPU limit.

    Records what it rules out as well as what it observes, which every other rule here
    already did — ``_latency_regression`` cites healthy gauges and quiet CPU, and
    ``_dependency_unavailable`` cites the reachable stores that narrow the blame. CPU was
    the one rule that reported only its own reading, so it always looked like a lone
    uncorroborated signal and drew the ``single_weak_signal`` penalty. The result was two
    *correct* CPU diagnoses reported as UNCERTAIN at 0.45 — underconfidence caused by an
    inconsistency between rules, not by weak evidence.

    Deliberately **not** counting the firing alert as a second source: the alert fires
    *from* this metric, so treating it as corroboration would double-count one signal, the
    same error ``ranker.py`` avoids by keeping severity out of its factors.
    """
    out = RuleOutcome()
    for pod in facts.saturated_cpu:
        out.supporting.append(
            (f"pod {pod.pod}: cpu={pod.cpu_cores:.2f} cores (limit 1)", "metrics")
        )
        out.component = out.component or pod.pod
    if out.supporting:
        out.supporting.extend(_log_support(facts, "cpu", "throttl"))
        if facts.gauges and not facts.unreachable_stores:
            out.supporting.append(
                ("every dependency gauge reads REACHABLE, ruling out a store outage", "metrics")
            )
        if facts.lifecycles and not any(life.died_without_oom for life in facts.lifecycles):
            out.supporting.append(
                ("no abnormal termination, so this is throttling rather than a crash", "metrics")
            )
    if not facts.saturated_cpu and facts.resources:
        highest = max((r.cpu_cores or 0.0) for r in facts.resources)
        out.contradicting.append(
            (f"no container above {highest:.2f} cores; none is CPU-saturated", "metrics")
        )
    return out


def _resource_exhaustion_memory(facts: ObservedFacts) -> RuleOutcome:
    """Memory pressure *without* an OOM kill.

    The pair this rule and :func:`_oom_kill` form is the one place restart count is the
    wrong discriminator. External memory pressure holds a container at its limit while
    the kernel reclaims, so it never OOMs and never restarts — meaning "no restarts"
    does not rule memory out. Only the termination reason separates the two.
    """
    out = RuleOutcome()
    for pod in facts.pressured_memory:
        out.supporting.append(
            (f"pod {pod.pod}: memory={pod.memory_ratio:.0%} of its limit", "metrics")
        )
        out.component = out.component or pod.pod
    if out.supporting:
        # Same narrowing the other rules record — see ``_resource_saturation_cpu``.
        out.supporting.extend(_log_support(facts, "memory", "alloc"))
        if facts.lifecycles and not any((life.restarts or 0) > 0 for life in facts.lifecycles):
            out.supporting.append(
                (
                    "memory is pinned at the limit with no restart, which is external "
                    "pressure rather than an in-process leak",
                    "metrics",
                )
            )
    if any(life.oom_killed for life in facts.lifecycles):
        out.contradicting.append(
            (
                "a container was OOMKilled, which points at an in-process leak rather than "
                "external pressure",
                "metrics",
            )
        )
    return out


def _oom_kill(facts: ObservedFacts) -> RuleOutcome:
    out = RuleOutcome()
    for life in facts.lifecycles:
        if life.oom_killed:
            out.supporting.append((f"pod {life.pod}: last terminated reason=OOMKilled", "metrics"))
            out.component = out.component or life.pod
            if (life.restarts or 0) > 0:
                out.supporting.append((f"pod {life.pod}: restartCount={life.restarts}", "metrics"))
    if out.supporting:
        for pod in facts.pressured_memory:
            out.supporting.append(
                (f"pod {pod.pod}: memory={pod.memory_ratio:.0%} of its limit", "metrics")
            )
        out.supporting.extend(_log_support(facts, "memory", "oom", "out of memory"))
    if facts.lifecycles and not any(life.oom_killed for life in facts.lifecycles):
        out.contradicting.append(("no container reports an OOMKilled termination", "metrics"))
    return out


def _process_crash_loop(facts: ObservedFacts) -> RuleOutcome:
    """A process dying for a non-OOM reason — typically before it can serve.

    Restarts alone are not enough: a pod restarted by an OOM kill is a memory problem,
    not a startup problem. The rule requires a non-OOM termination reason, and cites the
    absence of application log lines when it has it, because a process that dies before
    binding its port produces no HTTP logs at all.
    """
    out = RuleOutcome()
    for life in facts.lifecycles:
        if life.died_without_oom:
            out.supporting.append(
                (f"pod {life.pod}: last terminated reason={life.terminated_reason}", "metrics")
            )
            out.component = out.component or life.pod
        if (life.restarts or 0) > 0 and life.died_without_oom:
            out.supporting.append((f"pod {life.pod}: restartCount={life.restarts}", "metrics"))
    if out.supporting and facts.logs.absence_is_evidence and not facts.log_lines:
        out.supporting.append(
            (
                "no application log lines in the window, consistent with a process that dies "
                "before it serves traffic",
                "logs",
            )
        )
    if any(life.oom_killed for life in facts.lifecycles):
        out.contradicting.append(
            (
                "the termination reason is OOMKilled, which is memory exhaustion rather than a "
                "startup failure",
                "metrics",
            )
        )
    return out


def _latency_regression(facts: ObservedFacts) -> RuleOutcome:
    """A hop is slow while nothing is erroring or saturated.

    The residual explanation once the loud causes are excluded, so it names what it has
    ruled out. Without those exclusions it would fire on every incident that has any
    latency at all.
    """
    out = RuleOutcome()
    for latency in facts.latencies:
        if latency.breaches_threshold:
            out.supporting.append(
                (f"{latency.hop} p95 latency: {latency.seconds:.2f}s (above threshold)", "metrics")
            )
            out.component = out.component or latency.hop
    if out.supporting:
        if not facts.saturated_cpu and facts.resources:
            out.supporting.append(("no container is CPU-saturated", "metrics"))
        if facts.gauges and not facts.unreachable_stores:
            out.supporting.append(("every dependency gauge reads REACHABLE", "metrics"))
    if facts.saturated_cpu:
        out.contradicting.append(
            (
                "a container is CPU-saturated, so the latency is more likely a symptom of "
                "throttling than an independent regression",
                "metrics",
            )
        )
    if out.supporting and facts.trace_summary is not None and facts.trace_summary.slow_duration_ms:
        out.supporting.append(
            (
                f"traces: slowest recent trace root span {facts.trace_summary.slow_duration_ms:.0f}ms, "
                "corroborating the elevated latency end-to-end",
                "traces",
            )
        )
    return out


def _application_error_rate(facts: ObservedFacts) -> RuleOutcome:
    """The service is returning errors of its own, per the ``reason`` label.

    Excludes timeout reasons, which :func:`_dependency_timeout` owns — otherwise both
    rules fire on the same counter and the ranking cannot separate a callee problem from
    a caller problem.
    """
    out = RuleOutcome()
    timeouts = {id(r) for r in facts.reason_rates("timeout")}
    reasons: list[str] = []
    for rate in facts.error_rates:
        if id(rate) in timeouts:
            continue
        out.supporting.append((f"{rate.metric} reason={rate.reason}: {rate.rate:.3f}/s", "metrics"))
        reasons.append(rate.reason)
    if out.supporting:
        # The component is the ``reason`` label, not the service: that label is what names
        # the mechanism, and it is the only thing distinguishing "the service failed on its
        # own" from "a downstream rejected it".
        out.component = ", ".join(dict.fromkeys(reasons)) or None
        out.supporting.extend(_log_support(facts, "error", "failed", "exception", "500"))
    if facts.unreachable_stores:
        out.contradicting.append(
            (
                "a dependency gauge is down, so the errors are more likely a downstream "
                "consequence than an application fault",
                "metrics",
            )
        )
    # Traces are a third, independent evidence source — a real end-to-end request
    # record, not an inference. Only consulted once the metric/log evidence above
    # has already triggered this rule, mirroring how the dependency-gauge check
    # just above only contradicts an already-triggered rule rather than firing on
    # its own.
    if out.supporting and facts.trace_summary is not None:
        ts = facts.trace_summary
        if ts.has_errors:
            out.supporting.append(
                (f"traces: {ts.errors} of {ts.total} recent traces show an error status", "traces")
            )
        else:
            out.contradicting.append(
                (
                    f"traces: 0 of {ts.total} recent traces show an error status, which "
                    "narrows how far the fault actually reaches end-to-end requests",
                    "traces",
                )
            )
    return out


def _change_induced_regression(facts: ObservedFacts) -> RuleOutcome:
    """Placeholder for the change-correlation rule.

    Change evidence does not live in ``ObservedFacts`` — it arrives as commits and
    deployment records on the timeline — so this rule is evaluated by the pipeline with
    the timeline in hand rather than here. Present in the catalog so the class exists in
    one list, and returns nothing so it cannot fire on telemetry alone.
    """
    return RuleOutcome()


def _alert_stale_or_resolved(facts: ObservedFacts) -> RuleOutcome:
    """An alert is firing but the system currently looks healthy.

    The hypothesis nobody wants and everybody needs. An alert can be stale, replayed
    from cache, or already resolved, and the correct answer is then "no live fault" —
    not the most plausible-sounding mechanism. Requires ``CHECKED`` metrics: on an
    unavailable backend "nothing is wrong" is not an observation, it is the absence of
    one, and this rule must never fire on a blind spot.
    """
    out = RuleOutcome()
    if not facts.metrics.absence_is_evidence or not facts.alerts:
        return out
    quiet = (
        not facts.unreachable_stores
        and not facts.error_rates
        and not facts.saturated_cpu
        and not facts.pressured_memory
        and not any((life.restarts or 0) > 0 for life in facts.lifecycles)
        and not any(latency.breaches_threshold for latency in facts.latencies)
    )
    if quiet:
        names = ", ".join(alert.name for alert in facts.alerts)
        out.supporting.append(
            (f"{names} is firing, but every checked signal is within range", "metrics")
        )
        out.supporting.append(
            ("no dependency down, no error counter moving, no restarts, no saturation", "metrics")
        )
    return out


RULES: tuple[HypothesisRule, ...] = (
    HypothesisRule(
        rule_id="dependency_unavailable",
        label="A backing datastore is unreachable",
        category="dependency_unavailable",
        mechanism=(
            "The service cannot open a connection to {component}, so requests that need "
            "it fail while the service itself stays up"
        ),
        action_category="restore_dependency",
        needs=(NEED_GAUGES,),
        evaluate=_dependency_unavailable,
    ),
    HypothesisRule(
        rule_id="dependency_timeout",
        label="A downstream dependency is timing out",
        category="dependency_timeout",
        mechanism=(
            "Calls to {component} are not returning within the caller's budget, so requests "
            "stall and time out rather than failing fast"
        ),
        action_category="restore_downstream_latency",
        needs=(NEED_ERRORS, NEED_LATENCY),
        evaluate=_dependency_timeout,
    ),
    HypothesisRule(
        rule_id="resource_saturation_cpu",
        label="The container is CPU-saturated",
        category="resource_saturation_cpu",
        mechanism="{component} is running at or near its CPU limit and is being throttled",
        action_category="relieve_cpu_pressure",
        needs=(NEED_RESOURCES,),
        evaluate=_resource_saturation_cpu,
    ),
    HypothesisRule(
        rule_id="resource_exhaustion_memory",
        label="The container is under memory pressure without being OOM-killed",
        category="resource_exhaustion_memory",
        mechanism=(
            "{component} sits at its memory limit and the kernel is reclaiming, so it "
            "degrades without ever being killed or restarted"
        ),
        action_category="relieve_memory_pressure",
        needs=(NEED_RESOURCES, NEED_LIFECYCLE),
        evaluate=_resource_exhaustion_memory,
    ),
    HypothesisRule(
        rule_id="oom_kill",
        label="The container was killed for exceeding its memory limit",
        category="resource_exhaustion_memory_oom",
        mechanism="{component} grew past its memory limit and the kernel OOM-killed it",
        action_category="relieve_memory_pressure",
        needs=(NEED_LIFECYCLE,),
        evaluate=_oom_kill,
    ),
    HypothesisRule(
        rule_id="process_crash_loop",
        label="The process is failing to start and restarting",
        category="startup_failure",
        mechanism=(
            "{component} exits during startup before it can serve traffic, and is restarted "
            "repeatedly"
        ),
        action_category="repair_startup_configuration",
        needs=(NEED_LIFECYCLE,),
        evaluate=_process_crash_loop,
    ),
    HypothesisRule(
        rule_id="latency_regression",
        label="A request path has become slow",
        category="latency",
        mechanism=(
            "The {component} path is taking materially longer than its threshold while "
            "dependencies are healthy and no container is saturated"
        ),
        action_category="restore_path_latency",
        needs=(NEED_LATENCY,),
        evaluate=_latency_regression,
    ),
    HypothesisRule(
        rule_id="application_error_rate",
        label="The service is returning errors of its own",
        category="application_error",
        mechanism="The service is failing requests itself, reported as reason={component}",
        action_category="stop_application_errors",
        needs=(NEED_ERRORS,),
        evaluate=_application_error_rate,
    ),
    HypothesisRule(
        rule_id="change_induced_regression",
        label="A recent change preceded the incident",
        category="change_regression",
        mechanism="{component} was deployed or reconfigured shortly before onset",
        action_category="revert_change",
        needs=(NEED_CHANGES,),
        evaluate=_change_induced_regression,
    ),
    HypothesisRule(
        rule_id="alert_stale_or_resolved",
        label="No live fault: the alert appears stale or already resolved",
        category="stale_alert",
        mechanism=(
            "Every checked signal is within range, so the alert is likely stale, replayed, "
            "or already resolved rather than reporting a current fault"
        ),
        action_category="verify_and_close",
        needs=(NEED_GAUGES, NEED_ERRORS, NEED_LIFECYCLE, NEED_RESOURCES),
        evaluate=_alert_stale_or_resolved,
    ),
)

RULES_BY_ID = {rule.rule_id: rule for rule in RULES}

__all__ = [
    "NEED_CHANGES",
    "NEED_ERRORS",
    "NEED_GAUGES",
    "NEED_LATENCY",
    "NEED_LIFECYCLE",
    "NEED_RESOURCES",
    "RULES",
    "RULES_BY_ID",
    "HypothesisRule",
    "RuleOutcome",
]
