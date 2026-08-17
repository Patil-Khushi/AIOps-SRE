"""Service → failure-key mapping for RCA fix-step annotation.

Each ecommerce failure is injected by setting an env toggle or scaling a
datastore to zero; the reversible remediation is clearing that same fault. This
module is the backend source of truth for which keys belong to which service,
so the RCA agent can annotate a fix step with an executable action instead of
the dashboard re-deriving it.

Replaces the flagd map this file used to hold. That version mapped services to
OTel Demo feature flags (``paymentFailure``, ``productCatalogFailure``, …).
flagd and those services were removed in migration Phase 6, so every value in
it pointed at something that no longer exists — and because ``_normalise``
strips a trailing "service", ecommerce's ``payment-service`` collapsed to
``payment`` and resolved to the demo's ``paymentFailure``. A confidently wrong
executable remediation is worse than none.

Keys here are real failure keys from ``demo/ecommerce/failure_injection``:
they are what ``automation.fault.clear`` accepts, so a step annotated from this
map is one the executor can actually run.

Demoted to a fallback in Phase 4
--------------------------------
This list is no longer the source of truth for what the model may propose.
``agent._action_vocabulary`` asks the **action registry** first (via
``_live_flag_names``, which reads what ``automation.fault.clear`` actually accepts) and
falls back here only when the registry cannot be reached — offline, CI, no cluster.

The reason is the Q2 constraint: failure keys must not be hardcoded into RCA's
reasoning path. A static list goes stale silently, and the failure mode is specific —
a fault added to the platform is invisible to the agent until someone remembers to edit
a Python file, and a fault *removed* leaves the agent recommending a button that no
longer exists. The registry cannot drift from itself.

Kept because the alternative is worse: with no registry reachable, an empty vocabulary
tells the model there are no executable actions when there are, and every remediation
degrades to manual. So this list still serves the offline path — but wherever it is
used, the prompt says so, and ``is_valid_fault`` remains the validation backstop that
rejects an invented key before it reaches the executor.
"""

from __future__ import annotations

# Normalised base service name → the failure keys that service can exhibit.
#
# Ordered most-specific-cause first. A service has SEVERAL possible faults, so
# unlike the old one-flag-per-service map this cannot resolve to a single key
# from the service name alone — that is exactly why the agent must reason from
# evidence (agents/rca_agent/evidence.py) rather than look up a service name.
# The map's job is narrower now: validate that a key the model proposed is real
# and belongs to the service it named.
_SERVICE_FAULTS: dict[str, tuple[str, ...]] = {
    "user": (
        "user_service.mysql_down",
        "user_service.crashloop",
        "user_service.high_latency",
        "user_service.high_cpu",
    ),
    "order": (
        "order_service.postgres_down",
        "order_service.http_500",
        "order_service.memory_leak_oom",
        "order_service.payment_timeout",
    ),
    "payment": (
        "payment_service.redis_down",
        "payment_service.http_500",
        "payment_service.high_cpu",
        "payment_service.gateway_timeout",
    ),
    # The gateway has no faults keyed under its own name: its delay toggle is
    # registered under the two services that observe the timeout. Listed so a
    # fix step naming the gateway as the cause still validates.
    "mockpaymentgateway": (
        "order_service.payment_timeout",
        "payment_service.gateway_timeout",
    ),
}


def _normalise(service: str) -> str:
    """Collapse the many spellings of a service to a base key.

    Lower-cases, strips separators, then drops a trailing ``service`` suffix, so
    ``"order"``, ``"order-service"`` and ``"orderservice"`` all map to
    ``"order"``.
    """
    s = service.lower().strip()
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


def faults_for_service(service: str | None) -> tuple[str, ...]:
    """Every failure key ``service`` can exhibit. Empty tuple if unknown."""
    if not service:
        return ()
    return _SERVICE_FAULTS.get(_normalise(service), ())


def is_valid_fault(fault: str | None, service: str | None = None) -> bool:
    """Is ``fault`` a real failure key — and, if given, one this service has?

    Used to reject invented keys before they reach the executor. The LLM will
    otherwise coin plausible-looking handles (it produced ``orderServiceFailure``
    under the old flagd prompt), and an unrecognised key means the operator
    clicks Apply and gets "unknown fault" *after* already approving it.
    """
    if not fault:
        return False
    if service:
        return fault in faults_for_service(service)
    return any(fault in keys for keys in _SERVICE_FAULTS.values())


def flag_for_service(service: str | None) -> str | None:
    """Back-compat shim for the flagd-era call site. **No production caller.**

    Returns a key only when the service has exactly one candidate — never true for the
    ecommerce services, each of which has four. So it always returned ``None`` here, and
    the branch it guarded in ``agent._ensure_executable_action`` — the branch that
    *corrected* a wrong key — was unreachable dead code. Phase 5 deleted that call site.

    Kept so existing imports keep working, and because two tests assert the
    always-``None`` behaviour that made the dead branch visible in the first place. Do
    not reintroduce a caller: picking a remediation from a service name is the lookup
    this agent exists to replace, and with four candidates per service it cannot be done
    from the name at all. Use ``faults_for_service`` (all candidates) or
    ``is_valid_fault`` (validation).
    """
    keys = faults_for_service(service)
    return keys[0] if len(keys) == 1 else None


__all__ = ["faults_for_service", "flag_for_service", "is_valid_fault"]
