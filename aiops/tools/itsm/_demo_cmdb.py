"""Demo CMDB table — single source of truth for the POC's service ownership.

Used by two providers:

- ``aiops.tools.mock_providers.mock_cmdb_lookup`` — registered when
  ``AIOPS_USE_MOCK_ITSM=true`` (CI / tests / dev without a PDI).
- ``aiops.tools.itsm.servicenow.cmdb_lookup`` — registered when
  ``AIOPS_USE_MOCK_ITSM=false``. Real ServiceNow is queried first; when the
  PDI has no ``cmdb_ci_service`` row for a demo service (which is the common
  case — stock PDIs don't ship with Astronomy Shop CIs), we fall back to this
  table so ownership routing still works end-to-end (DEMO-1 / #53).

This is intentionally NOT a tool capability of its own. Callers reach in via
the public ``lookup(service)`` function; nobody outside the two ITSM provider
modules should import it.
"""

from __future__ import annotations

# Service -> {team, runbook}. Lowercase keys; callers lowercase + strip first.
# Covers the OpenTelemetry Astronomy Shop services + a few generic aliases
# ("payment-api" matches canonical example inputs).
CMDB_TABLE: dict[str, dict[str, str | None]] = {
    "payment": {"team": "Payments Team", "runbook": "https://runbooks.example.com/payment-cpu"},
    "payment-api": {"team": "Payments Team", "runbook": "https://runbooks.example.com/payment-cpu"},
    "payment-service": {
        "team": "Payments Team",
        "runbook": "https://runbooks.example.com/payment-cpu",
    },
    "cart": {"team": "Order Experience", "runbook": "https://runbooks.example.com/cart"},
    "checkout": {"team": "Order Experience", "runbook": "https://runbooks.example.com/checkout"},
    "product-catalog": {"team": "Catalog Team", "runbook": "https://runbooks.example.com/catalog"},
    "product-reviews": {"team": "Catalog Team", "runbook": None},
    "recommendation": {
        "team": "Personalization Team",
        "runbook": "https://runbooks.example.com/recommendation",
    },
    "frontend": {"team": "Web Experience", "runbook": "https://runbooks.example.com/frontend"},
    "frontend-proxy": {"team": "Web Experience", "runbook": None},
    "shipping": {"team": "Fulfillment Team", "runbook": "https://runbooks.example.com/shipping"},
    "ad": {"team": "Ads Team", "runbook": None},
    "quote": {"team": "Pricing Team", "runbook": None},
    "currency": {"team": "Pricing Team", "runbook": None},
    "fraud-detection": {"team": "Trust and Safety", "runbook": None},
    "email": {"team": "Communications", "runbook": None},
    "accounting": {"team": "Finance Systems", "runbook": None},
    "image-provider": {"team": "Assets Team", "runbook": None},
}

CMDB_DEFAULT: dict[str, str | None] = {"team": "Platform On-Call", "runbook": None}


def lookup(service: str) -> dict[str, str | None] | None:
    """Return ``{team, runbook}`` for ``service`` or ``None`` if not in the table.

    Returning ``None`` (rather than the ``Platform On-Call`` default) lets
    callers distinguish "service is known, has no runbook" from "service is
    unknown" — important for the ServiceNow fallback path where we want to
    keep the upstream PDI's no-match signal visible in the audit trail.
    """
    return CMDB_TABLE.get((service or "").lower().strip())
