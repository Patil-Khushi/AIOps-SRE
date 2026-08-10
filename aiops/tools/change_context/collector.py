"""Change-context collection: fan out across providers and merge.

A **union**, deliberately unlike the topology and history chains. Those answer one
question where the best available source wins. Here a GitHub commit, a flag flip
and a Kubernetes rollout can all be true simultaneously, and stopping at the first
provider that returned something would discard most of the change picture — which
during an incident is the part a responder most wants.

Every provider is attempted (subject to a total budget) and unavailable sources are
reported by name, so an empty record list can be read correctly: "nothing changed"
only when every source actually answered.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from aiops.tools.change_context.base import (
    ChangeContext,
    ChangeContextProvider,
    ChangeContextResult,
    ProviderStatus,
)
from aiops.tools.change_context.providers import (
    ArgoCDChangeProvider,
    FeatureFlagChangeProvider,
    GitHubChangeProvider,
    GitLabChangeProvider,
    JenkinsChangeProvider,
    KubernetesRolloutChangeProvider,
)
from aiops.tools.resilience import ResiliencePolicy, guard
from aiops.tools.resilience import reset_for_tests as _reset_resilience

logger = logging.getLogger(__name__)

# All seven sources are represented; the three absent platforms self-report as
# unavailable rather than being omitted, so their absence is visible rather than
# silent.
_PROVIDERS: dict[str, ChangeContextProvider] = {
    "github": GitHubChangeProvider(),
    "gitlab": GitLabChangeProvider(),
    "argocd": ArgoCDChangeProvider(),
    "jenkins": JenkinsChangeProvider(),
    "feature_flags": FeatureFlagChangeProvider(),
    "kubernetes": KubernetesRolloutChangeProvider(),
}

_DEFAULT_CHAIN = "github,feature_flags,kubernetes"
"""Default is the three sources that genuinely exist here.

The absent platforms are excluded by default rather than probed: attempting a
connection to a GitLab that was never deployed adds latency to every correlation
to learn something already known from configuration.
"""

_TOTAL_BUDGET = float(os.environ.get("AIOPS_CHANGE_CONTEXT_BUDGET", "8"))

# Longer cache TTL than the other seams: deployments and commits change on human
# timescales, not per-request, so re-querying git and the Kubernetes API for every
# correlation in an incident is pure latency for an answer that has not moved.
_POLICY = ResiliencePolicy(
    timeout=float(os.environ.get("AIOPS_CHANGE_CONTEXT_PROVIDER_TIMEOUT", "5")),
    retries=int(os.environ.get("AIOPS_CHANGE_CONTEXT_RETRIES", "1")),
    breaker_seconds=float(os.environ.get("AIOPS_CHANGE_CONTEXT_BREAKER", "60")),
    cache_ttl=float(os.environ.get("AIOPS_CHANGE_CONTEXT_CACHE_TTL", "180")),
)


def register_provider(provider: ChangeContextProvider) -> None:
    _PROVIDERS[provider.name] = provider


def reset_for_tests() -> None:
    """Clear breaker and cache state via the shared middleware."""
    _reset_resilience()


def _chain() -> tuple[list[str], list[str]]:
    """Split the configured chain into ``(known, unknown)`` provider names.

    Unknown names are *returned*, not merely logged. Dropping them silently made a
    typo indistinguishable from a clean result: with every configured name
    unrecognised the loop below never ran, ``sources_unavailable`` stayed empty,
    and ``ChangeContext.complete`` came back ``True`` — reporting "checked
    everything, nothing changed" for a chain that checked nothing. A log warning is
    not enough, because the caller reads the returned object, not the log.
    """
    raw = os.environ.get("AIOPS_CHANGE_CONTEXT_PROVIDERS", "").strip() or _DEFAULT_CHAIN
    names = [n.strip() for n in raw.split(",") if n.strip()]
    known: list[str] = []
    unknown: list[str] = []
    for n in names:
        if n in _PROVIDERS:
            known.append(n)
        else:
            logger.warning("change_context: unknown provider %r; skipping", n)
            unknown.append(n)
    return known, unknown


def collect_change_context(
    service: str, window_start: datetime, window_end: datetime
) -> ChangeContext:
    """Gather deployment and configuration changes from every enabled provider.

    Returns facts only: what changed, when, and by whom. It makes no claim that any
    change caused the incident and applies no suspicion ordering — records are
    sorted chronologically, because time is a fact and relevance is a judgement.

    Never raises.
    """
    started = time.monotonic()
    attempts: list[ChangeContextResult] = []

    chain, unknown = _chain()

    # A name that resolves to no provider is a coverage hole, so it is recorded as
    # one. Otherwise a typo in AIOPS_CHANGE_CONTEXT_PROVIDERS yields an authoritative
    # "nothing changed" — the exact false-completeness this module warns against.
    for name in unknown:
        attempts.append(
            ChangeContextResult(
                provider=name,
                status=ProviderStatus.UNAVAILABLE,
                note="unknown provider name; check AIOPS_CHANGE_CONTEXT_PROVIDERS",
            )
        )

    # A chain that parses to nothing at all is the same false-completeness by a
    # different route: AIOPS_CHANGE_CONTEXT_PROVIDERS="," is non-empty so the default
    # chain is not substituted, yet every element is dropped as blank — leaving zero
    # attempts, zero unavailable sources and therefore complete=True from a collector
    # that asked nobody. Recording the misconfiguration keeps that impossible.
    if not chain and not unknown:
        attempts.append(
            ChangeContextResult(
                provider="(none)",
                status=ProviderStatus.UNAVAILABLE,
                note=(
                    "no change-context provider configured; "
                    "AIOPS_CHANGE_CONTEXT_PROVIDERS parsed to an empty chain"
                ),
            )
        )

    for name in chain:
        if (time.monotonic() - started) >= _TOTAL_BUDGET:
            attempts.append(
                ChangeContextResult(
                    provider=name,
                    status=ProviderStatus.UNAVAILABLE,
                    note="change-context budget exhausted before this provider",
                )
            )
            continue

        provider = _PROVIDERS[name]
        try:
            healthy, detail = provider.health()
        except Exception as exc:
            healthy, detail = False, f"health check raised: {type(exc).__name__}"
        if not healthy:
            attempts.append(
                ChangeContextResult(provider=name, status=ProviderStatus.UNAVAILABLE, note=detail)
            )
            continue

        # Guarded: this seam had a timeout but no circuit breaker and no cache, so a
        # hanging GitLab would be retried on every single incident with nothing
        # remembered between them. The middleware supplies breaker, cache and
        # retries; the enforced timeout also bounds providers that ignore their own.
        outcome = guard(
            f"change_context.{name}",
            lambda p=provider: p.collect(service, window_start, window_end),
            policy=_POLICY,
            cache_key=(
                f"change:{name}:{service}:{window_start.isoformat()}:{window_end.isoformat()}"
            ),
            is_transient=lambda r: r.status is ProviderStatus.FAILED,
            is_cacheable=lambda r: r.status in (ProviderStatus.COLLECTED, ProviderStatus.EMPTY),
            is_empty=lambda r: r.status is ProviderStatus.EMPTY,
        )

        if outcome.value is not None:
            attempts.append(outcome.value)
        else:
            attempts.append(
                ChangeContextResult(
                    provider=name,
                    # A breaker skip is "we chose not to ask" and starvation is "we
                    # never got to ask" — both availability facts rather than
                    # failures of this provider. Only a real call that went wrong
                    # is FAILED, which is what the breaker keys off.
                    status=ProviderStatus.UNAVAILABLE
                    if (outcome.breaker_open or outcome.starved)
                    else ProviderStatus.FAILED,
                    error=outcome.error,
                    note="; ".join(outcome.notes) or None,
                    latency_ms=outcome.latency_ms,
                )
            )

    records = [r for a in attempts for r in a.records]
    # Chronological, with a stable secondary key so equal timestamps do not
    # reorder between runs. Explicitly *not* sorted by suspicion — ranking changes
    # by blame is the inference this seam refuses to make.
    records.sort(
        key=lambda r: (r.timestamp or datetime.min.replace(tzinfo=None), r.source, r.change_id)
    )

    collected = [a.provider for a in attempts if a.collected]
    unavailable = [
        a.provider
        for a in attempts
        if a.status in (ProviderStatus.UNAVAILABLE, ProviderStatus.FAILED)
    ]
    notes = [f"{a.provider}: {a.note or a.error}" for a in attempts if a.note or a.error]

    return ChangeContext(
        records=records,
        sources_collected=sorted(collected),
        sources_unavailable=sorted(unavailable),
        coverage_note="; ".join(notes) or None,
    )
