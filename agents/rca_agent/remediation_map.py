"""Service → flagd failure-flag mapping for RCA fix-step annotation.

Each OTel-demo failure scenario is injected by flipping a flagd feature flag
``on``; the real, reversible remediation is flipping that same flag ``off``.
This module is the *backend* source of truth for that mapping so the RCA
agent can annotate a fix step with an executable action (``set_flag`` +
``flag``) instead of the dashboard re-deriving it from a hardcoded map.

Keep in sync with ``demo/dashboard/src/pages/AlertStream.tsx`` (``SERVICE_FLAG``).
The dashboard map stays as a UI-side fallback for verdicts that predate
step-level action annotation; this one is what the agent uses.
"""

from __future__ import annotations

# Normalised base service name → the flagd flag whose flip is the fix. Values
# are *real* flagd flag names (verified against the OTel-demo flagd config), so
# they are authoritative over a flag the LLM guesses from the <service>Failure
# pattern (it sometimes guesses wrong — e.g. 'recommendationFailure' instead of
# the real 'recommendationCacheFailure').
_SERVICE_FLAG: dict[str, str] = {
    "payment": "paymentFailure",
    "productcatalog": "productCatalogFailure",
    "cart": "cartFailure",
    "ad": "adFailure",
    "recommendation": "recommendationCacheFailure",
}


def _normalise(service: str) -> str:
    """Collapse the many spellings of a service to a base key.

    Lower-cases, strips separators (``-``, ``_``, spaces), then drops a
    trailing ``service`` suffix, so ``"recommendation"``,
    ``"recommendationservice"``, and ``"recommendation-service"`` all map to
    ``"recommendation"``. This is why the map only needs base keys.
    """
    s = service.lower().strip()
    for sep in ("-", "_", " "):
        s = s.replace(sep, "")
    if s.endswith("service") and len(s) > len("service"):
        s = s[: -len("service")]
    return s


def flag_for_service(service: str | None) -> str | None:
    """Return the flagd failure-flag for ``service``, or ``None`` if unmapped.

    ``None`` means "no known one-flag remediation" — the agent leaves the
    step as a manual action rather than inventing a flag the executor can't
    safely flip.
    """
    if not service:
        return None
    return _SERVICE_FLAG.get(_normalise(service))
