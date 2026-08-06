"""Static catalog of remediation patterns the agent adds *on top of* RCA.

RCA produces fix-steps *for the diagnosed cause*. This catalog adds
**symptom-driven mitigations** RCA may not have proposed — circuit
breakers, rate-limiters, fail-over flags, and other "stop the bleeding"
options that don't fix the root cause but reduce blast radius while
someone investigates.

Day-1 catalog is intentionally small (one row per failure family) so
the contract is easy to read and the eval goldens stay tractable. v1
expands this to a per-service lookup keyed by CMDB metadata.

The catalog is **pure data** — no LLM, no DB. The agent looks up
``patterns_for_cause(root_cause)`` and merges the returned templates
into the option list. If no row matches, the agent falls back to
deriving options 1:1 from RCA's fix_steps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ActionType, BlastRadius


@dataclass(frozen=True)
class CatalogOption:
    """A pattern-driven remediation template.

    ``confidence`` is the catalog-author's prior — a hand-curated baseline
    before the agent's ranking step adjusts it for environment, operator
    preference, and blast radius. ``estimated_mttr_minutes`` is the
    median-case wall-clock for a trained on-call to execute the option,
    not including approval latency.
    """

    option_id: str
    title: str
    description: str
    action_type: ActionType
    blast_radius: BlastRadius
    rollback: str
    rollback_tested: bool
    confidence: float
    estimated_mttr_minutes: int
    rationale: str
    tool_capability: str | None = None
    # Templated tool args — the agent fills in service / flag names from
    # the RCA verdict at runtime before publishing.
    tool_args_template: dict | None = None


# Lookup is keyed by case-folded substrings of ``RCAVerdict.root_cause``.
# A root cause matches a pattern if ANY of the pattern's keyword tuples
# is fully contained (set-intersection) in the cause's tokens.
#
# Adding a pattern: append below + cover with at least one golden case.
_PATTERNS: list[tuple[tuple[str, ...], list[CatalogOption]]] = [
    # ── Kafka consumer lag / partition rebalance ────────────────────────
    (
        ("kafka", "lag"),
        [
            CatalogOption(
                option_id="kafka-restart-consumer",
                title="Restart the Kafka consumer group",
                description="Bounce the consumer pods so the group re-balances and resumes from the latest committed offset.",
                action_type=ActionType.RESTART,
                blast_radius=BlastRadius.MEDIUM,
                rollback="Pods auto-restart; no rollback needed beyond watching offset catch-up.",
                rollback_tested=True,
                confidence=0.7,
                estimated_mttr_minutes=8,
                rationale="Catalog: bouncing the consumer group is the safest first move when lag is climbing — clears stuck partitions without touching brokers or data.",
                tool_capability="k8s.deployment.restart",
                tool_args_template={"namespace": "otel-demo", "deployment": "{service}"},
            ),
            CatalogOption(
                option_id="kafka-circuit-breaker-disable-producer",
                title="Open circuit breaker on the upstream producer",
                description="Flip the producer to fail-fast / queue-locally while the consumer catches up, instead of back-pressuring the whole pipeline.",
                action_type=ActionType.CIRCUIT_BREAKER,
                blast_radius=BlastRadius.LOW,
                rollback="Flip the same flag back to ``off``.",
                rollback_tested=True,
                confidence=0.5,
                estimated_mttr_minutes=2,
                rationale="Catalog: stops the bleeding without touching the broker; loses data unless the producer buffers locally.",
                tool_capability="automation.fault.clear",
                tool_args_template={"flag": "{service}ProducerCircuitBreaker", "variant": "on"},
            ),
        ],
    ),
    # ── Connection pool exhausted (DB / HTTP client) ─────────────────────
    (
        ("connection", "pool"),
        [
            CatalogOption(
                option_id="connection-pool-bump-size",
                title="Increase connection pool size",
                description="Bump the pool ceiling for this service — short-term relief while the long-lived connections drain.",
                action_type=ActionType.SET_FLAG,
                blast_radius=BlastRadius.LOW,
                rollback="Revert the flag to its previous numeric value.",
                rollback_tested=False,
                confidence=0.55,
                estimated_mttr_minutes=3,
                rationale="Catalog: lowest-blast-radius lever when a pool is saturated; doesn't address why connections are leaking.",
                tool_capability="automation.fault.clear",
                tool_args_template={"flag": "{service}DbPoolSize", "variant": "doubled"},
            ),
        ],
    ),
    # ── Memory pressure / OOM kill ──────────────────────────────────────
    (
        ("memory", "oom"),
        [
            CatalogOption(
                option_id="memory-rollback-recent-deploy",
                title="Roll back the most recent deploy",
                description="If the OOM started after a deploy, rolling back is faster than diagnosing the leak.",
                action_type=ActionType.ROLLBACK_DEPLOY,
                blast_radius=BlastRadius.MEDIUM,
                rollback="Re-deploy the current image with explicit version pin.",
                rollback_tested=False,
                confidence=0.6,
                estimated_mttr_minutes=12,
                rationale="Catalog: change-correlated OOMs revert cleanly via Helm; no executor wired in v0 so this surfaces as a manual instruction.",
                tool_capability=None,
                tool_args_template=None,
            ),
        ],
    ),
    # ── Third-party / external dependency (e.g. payment gateway 5xx) ─────
    (
        ("external", "dependency"),
        [
            CatalogOption(
                option_id="external-fail-open-flag",
                title="Fail open to the secondary provider",
                description="Flip the gateway selector to the backup provider until the primary recovers.",
                action_type=ActionType.SET_FLAG,
                blast_radius=BlastRadius.LOW,
                rollback="Flip the selector flag back to ``primary``.",
                rollback_tested=True,
                confidence=0.65,
                estimated_mttr_minutes=2,
                rationale="Catalog: external-dependency outages have no internal fix; cheapest mitigation is provider switch when a backup exists.",
                tool_capability="automation.fault.clear",
                tool_args_template={"flag": "{service}GatewayProvider", "variant": "secondary"},
            ),
        ],
    ),
    # ── Cart / Redis / Valkey state issues ──────────────────────────────
    (
        ("cart", "redis"),
        [
            CatalogOption(
                option_id="cart-flush-stale-sessions",
                title="Flush stale cart sessions",
                description="Drop sessions older than the configured TTL to free the working set.",
                action_type=ActionType.MANUAL,
                blast_radius=BlastRadius.LOW,
                rollback="Sessions are write-through; rollback is not applicable.",
                rollback_tested=True,
                confidence=0.5,
                estimated_mttr_minutes=4,
                rationale="Catalog: a manual REDIS SCAN + DEL is the safest first move when working-set pressure shows on the cart service.",
                tool_capability=None,
                tool_args_template=None,
            ),
        ],
    ),
]


def patterns_for_cause(root_cause: str) -> list[CatalogOption]:
    """Return catalog options whose keyword tuple matches the cause text.

    Case-folded, substring-based matching. Returns all matching patterns;
    the agent merges them into the option list and de-duplicates by
    ``option_id``.
    """
    if not root_cause:
        return []
    cause_lower = root_cause.lower()
    matched: list[CatalogOption] = []
    for keywords, options in _PATTERNS:
        if all(kw in cause_lower for kw in keywords):
            matched.extend(options)
    return matched


__all__ = ["CatalogOption", "patterns_for_cause"]
