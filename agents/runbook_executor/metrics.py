"""In-process counters for the Runbook Executor (§31).

Deliberately dependency-free. ``prometheus_client`` ships only in the ``ui`` extra, and
CI installs ``dev`` + ``ui`` but agents must import cleanly without either — so this is
a dict of integers plus a bounded duration list, exposed as JSON through
``GET /api/runbook-executor/metrics``. That matches how the rest of this repo surfaces
agent metrics (``/api/classifier/metrics``, ``/api/war-room/metrics``) rather than
introducing a second telemetry story for one agent.

Every rate in :func:`snapshot` names its own numerator and denominator, because a "rate"
with an unstated denominator is the thing that makes dashboards lie. Rates over an empty
denominator are reported as ``None``, never as ``0.0``: nothing having happened yet is
not the same as it having failed.

Process-global state, so ``tests/conftest.py`` resets it between tests (the repo has
already paid for module-level caches that leaked across tests, #113).
"""

from __future__ import annotations

import threading
from typing import Any

# Bound the duration sample so a long-running demo cannot grow it without limit.
_MAX_SAMPLES = 512

_LOCK = threading.Lock()
_COUNTERS: dict[str, int] = {}
_DURATIONS: dict[str, list[float]] = {}
# (counter, key) pairs already counted, for observations that arrive repeatedly.
# The verification verdict is read on every poll of an execution; counting it each
# time would turn one verified incident into a hundred, which is how a pass-rate
# stops meaning anything.
_SEEN: set[tuple[str, str]] = set()

# Every counter this module knows about, so a snapshot has stable keys from the first
# request (a dashboard should not have to handle a missing series).
COUNTER_NAMES: tuple[str, ...] = (
    # discovery
    "discovery_total",
    "discovery_auto_select",
    "discovery_candidates",
    "discovery_no_runbook",
    "discovery_ambiguous",
    "discovery_not_applicable",
    "discovery_blocked",
    # selection
    "selection_auto",
    "selection_manual",
    "selection_rejected",
    # dry run
    "dry_run_total",
    "dry_run_ready",
    "dry_run_blocked",
    # execution
    "execution_requested",
    "execution_started",
    "execution_completed",
    "execution_failed",
    "execution_rolled_back",
    "execution_blocked",
    "execution_duplicate",
    "execution_stale_blocked",
    "execution_lease_conflict",
    "execution_policy_blocked",
    "rollback_attempted",
    "rollback_failed",
    # HITL
    "hitl_required",
    "hitl_approved",
    "hitl_rejected",
    # verification (recorded by the surface that observes the verifier)
    "verification_pass",
    "verification_fail",
)


def incr(name: str, amount: int = 1) -> None:
    """Add to a counter. Unknown names are accepted and reported (forward-compatible)."""
    with _LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + amount


def observe(name: str, value: float) -> None:
    """Record a duration sample (ms). Oldest samples are dropped past the cap."""
    with _LOCK:
        series = _DURATIONS.setdefault(name, [])
        series.append(float(value))
        if len(series) > _MAX_SAMPLES:
            del series[: len(series) - _MAX_SAMPLES]


def incr_once(name: str, key: str, amount: int = 1) -> bool:
    """Increment ``name`` at most once for ``key``. True when it actually counted."""
    with _LOCK:
        marker = (name, key)
        if marker in _SEEN:
            return False
        _SEEN.add(marker)
        _COUNTERS[name] = _COUNTERS.get(name, 0) + amount
        return True


def counter(name: str) -> int:
    with _LOCK:
        return _COUNTERS.get(name, 0)


def reset() -> None:
    """Zero everything. Test hook — wired into ``tests/conftest.py``."""
    with _LOCK:
        _COUNTERS.clear()
        _DURATIONS.clear()
        _SEEN.clear()


def _rate(numerator: int, denominator: int) -> float | None:
    """``None`` when nothing has happened yet — see the module docstring."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def snapshot() -> dict[str, Any]:
    """Counters, derived rates and average durations, JSON-safe."""
    with _LOCK:
        counters = {name: _COUNTERS.get(name, 0) for name in COUNTER_NAMES}
        extra = {k: v for k, v in _COUNTERS.items() if k not in counters}
        durations = {
            name: {
                "count": len(series),
                "avg_ms": round(sum(series) / len(series), 2) if series else None,
                "max_ms": round(max(series), 2) if series else None,
            }
            for name, series in _DURATIONS.items()
        }

    discovery = counters["discovery_total"]
    dry_runs = counters["dry_run_total"]
    executions = counters["execution_started"]
    selections = counters["selection_auto"] + counters["selection_manual"]
    hitl_decisions = counters["hitl_approved"] + counters["hitl_rejected"]
    verifications = counters["verification_pass"] + counters["verification_fail"]
    matched = discovery - counters["discovery_no_runbook"]

    return {
        "counters": {**counters, **extra},
        "durations": durations,
        "rates": {
            # denominator: every discovery pass
            "runbook_match_rate": _rate(matched, discovery),
            "no_runbook_rate": _rate(counters["discovery_no_runbook"], discovery),
            "ambiguous_match_rate": _rate(counters["discovery_ambiguous"], discovery),
            "not_applicable_rate": _rate(counters["discovery_not_applicable"], discovery),
            "candidate_selection_rate": _rate(counters["discovery_candidates"], discovery),
            # denominator: every selection that happened
            "automatic_selection_rate": _rate(counters["selection_auto"], selections),
            "manual_selection_rate": _rate(counters["selection_manual"], selections),
            # denominator: every dry run
            "dry_run_success_rate": _rate(counters["dry_run_ready"], dry_runs),
            "dry_run_block_rate": _rate(counters["dry_run_blocked"], dry_runs),
            # denominator: every execution that actually started
            "execution_success_rate": _rate(counters["execution_completed"], executions),
            "execution_failure_rate": _rate(counters["execution_failed"], executions),
            "rollback_rate": _rate(counters["execution_rolled_back"], executions),
            # denominator: every execution REQUEST (includes the ones refused up front)
            "duplicate_execution_rate": _rate(
                counters["execution_duplicate"], counters["execution_requested"]
            ),
            "stale_execution_attempt_rate": _rate(
                counters["execution_stale_blocked"], counters["execution_requested"]
            ),
            "policy_block_rate": _rate(
                counters["execution_policy_blocked"], counters["execution_requested"]
            ),
            # denominator: every resolved HITL decision
            "hitl_approval_rate": _rate(counters["hitl_approved"], hitl_decisions),
            "hitl_rejection_rate": _rate(counters["hitl_rejected"], hitl_decisions),
            # denominator: every verification the executor was told about
            "verification_pass_rate_after_execution": _rate(
                counters["verification_pass"], verifications
            ),
        },
        "average_execution_time_ms": durations.get("execution_duration", {}).get("avg_ms"),
    }


__all__ = ["COUNTER_NAMES", "counter", "incr", "incr_once", "observe", "reset", "snapshot"]
