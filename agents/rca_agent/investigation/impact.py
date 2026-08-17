"""Stage 9 — blast radius: who else is affected, and who we simply did not look at.

The distinction this stage exists to preserve
---------------------------------------------
"We checked and it is healthy" and "we never checked" are different facts, and a blast
radius that conflates them is worse than none: it tells an incident commander that a
service is fine when nobody looked. ``ImpactState`` keeps five states for that reason,
and the two that matter most here are ``OBSERVED_HEALTHY`` (a gauge or metric was read
and it was normal) and ``NOT_OBSERVED`` (in the topology, never queried).

So this module never infers health from silence. A service appears as
``OBSERVED_HEALTHY`` only when an observation supports it, and every service the topology
names but the evidence does not cover is reported as ``NOT_OBSERVED`` — visibly, in the
report, rather than omitted.

Two different things called "blast radius"
-----------------------------------------
This report is **incident spread**: how far the failure has reached.
``RankedFixStep.blast_radius`` is **action risk**: how much damage the proposed fix could
do. They are not the same question and must not be collapsed — a tiny fix to a
widely-spread outage is low action-risk and high incident-spread. The recovery stage sets
the action value from the risk assessment; this report is carried structurally on
``Investigation.blast_radius`` and is not summarised into an enum, because
``RCAVerdict`` has no incident-level field to summarise it into and inventing one would
give the dashboard two similarly-named numbers meaning different things.

Nothing here reports cluster scope either. RCA reads one service's telemetry plus the
datastores it names, so "cluster-wide" is outside what this evidence can establish, and a
report that claimed it would be manufacturing alarm from a blind spot.
"""

from __future__ import annotations

from typing import Any

from agents.rca_agent.investigation.facts import ObservedFacts
from agents.rca_agent.investigation.models import (
    BlastRadiusReport,
    ImpactState,
    IncidentScope,
    ServiceImpact,
)

# ``ServiceImpact.relation`` is a plain string, and these are the values the context
# layer's correlator already writes into ``Observation.metadata["topology_relation"]``.
# Reused verbatim rather than given a local enum so a consumer reading both sources sees
# one vocabulary — a second spelling of "dependency" would be a silent mismatch.
_SELF = "self"
_DEPENDENCY = "dependency"


def _dependency_label(raw: str) -> str:
    """Strip the ``"Redis (payment-service)"`` decoration down to the store name.

    Gauge labels carry the owning service in parentheses, which is useful in prose and
    unhelpful as an identity: the same store rendered two ways would appear twice in the
    report.
    """
    return raw.split("(")[0].strip() or raw.strip()


def build_blast_radius(
    scope: IncidentScope,
    facts: ObservedFacts,
    *,
    context: dict[str, Any] | None = None,
) -> BlastRadiusReport:
    """Assemble the impact picture from observed facts plus whatever topology exists.

    Pure. Reads only the facts already collected and the Context Pack already built, so
    it issues no queries of its own and cannot fail an incident.
    """
    impacts: list[ServiceImpact] = []
    seen: set[str] = set()
    service = scope.affected_service

    # 1. The service the alert names. Directly affected when anything was observed on
    #    it; UNKNOWN when nothing was — not "healthy", because an alert fired.
    if facts.any_observation:
        symptoms = scope.user_visible_symptom
        impacts.append(
            ServiceImpact(
                service=service,
                state=ImpactState.DIRECTLY_AFFECTED,
                relation=_SELF,
                hops=0,
                rationale=f"the alerting service, and its own telemetry shows: {symptoms}",
            )
        )
    else:
        impacts.append(
            ServiceImpact(
                service=service,
                state=ImpactState.UNKNOWN,
                relation=_SELF,
                hops=0,
                rationale=(
                    "the alerting service, but no telemetry was observed — impact is "
                    "unestablished rather than absent"
                ),
            )
        )
    seen.add(service.lower())

    # 2. Datastores this service reports on. A gauge is the service's *own* view of
    #    whether it can reach the store, so an unreachable one is directly implicated
    #    and a reachable one is genuinely observed healthy.
    for gauge in facts.gauges:
        label = _dependency_label(gauge.label or gauge.metric)
        if label.lower() in seen:
            continue
        seen.add(label.lower())
        impacts.append(
            ServiceImpact(
                service=label,
                state=(
                    ImpactState.OBSERVED_HEALTHY
                    if gauge.reachable
                    else ImpactState.DIRECTLY_AFFECTED
                ),
                relation=_DEPENDENCY,
                hops=1,
                rationale=(
                    f"{service} reports {label} as "
                    + ("reachable" if gauge.reachable else "unreachable")
                    + f" ({gauge.metric}={gauge.value:g})"
                ),
            )
        )

    # 3. Topology neighbours nobody queried. Reported rather than dropped: an
    #    unexamined dependent is the most likely place for undetected user impact, and
    #    omitting it reads as "nothing else is affected".
    topology = tuple(scope.initial_blast_radius)
    for neighbour in topology:
        if neighbour.lower() in seen:
            continue
        seen.add(neighbour.lower())
        impacts.append(
            ServiceImpact(
                service=neighbour,
                state=ImpactState.NOT_OBSERVED,
                relation=_DEPENDENCY,
                hops=1,
                rationale=(
                    "named as a dependency by the topology, but no telemetry for it was "
                    "collected — impact unknown, not ruled out"
                ),
            )
        )

    endpoints = _affected_endpoints(facts)
    topology_available = bool(topology) or _has_topology_section(context)
    note = None
    if not topology_available:
        note = (
            "no topology was available, so only the alerting service and the datastores "
            "it reports on could be assessed. Dependents are neither listed nor ruled out."
        )

    return BlastRadiusReport(
        impacts=tuple(impacts),
        affected_endpoints=endpoints,
        topology_available=topology_available,
        note=note,
    )


def _has_topology_section(context: dict[str, Any] | None) -> bool:
    if not isinstance(context, dict):
        return False
    section = context.get("topology")
    return isinstance(section, dict) and bool(section.get("raw"))


def _affected_endpoints(facts: ObservedFacts) -> tuple[str, ...]:
    """Endpoints implicated by an observed latency breach or error counter.

    Derived from the hop and metric names the evidence actually carries, so an endpoint
    appears only when something was measured on it.
    """
    endpoints: list[str] = []
    for latency in facts.latencies:
        if latency.breaches_threshold and latency.hop:
            endpoints.append(latency.hop)
    for rate in facts.error_rates:
        if rate.rate > 0 and rate.metric:
            endpoints.append(rate.metric)
    out: list[str] = []
    for endpoint in endpoints:
        if endpoint not in out:
            out.append(endpoint)
    return tuple(out)


__all__ = ["build_blast_radius"]
