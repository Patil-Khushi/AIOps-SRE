"""PagerDuty Events API v2 adapter for the chatops seam (CHAT-5, issue #85).

Turns RA-005's ``page_oncall`` routing action into an actual PagerDuty
incident that wakes the on-call engineer. Without this adapter,
``page_oncall`` is metadata only — the Sev-1 demo line is a verbal claim.

Filter contract:
    This adapter only acts on ``ChatMessage`` instances whose ``actions``
    list contains ``"page_oncall"``. Chat-only sends (Sev-3 daytime,
    Sev-4 noise, Sev-2 business-hours team chat) are silently skipped so
    they don't create PD incidents for low-severity noise.

    The routing decision is the source of truth for "this should page."
    If a future scenario adds new page-worthy actions, extend
    :data:`PAGE_ACTIONS` rather than gating on severity here — that would
    move policy out of RA-005.

Setup (one-time, ~10 min, free):

1. Sign up for a PagerDuty developer account at developer.pagerduty.com
   (no credit card required).
2. Create a service in the PD dashboard.
3. Attach an "Events API v2" integration to it.
4. Copy the 32-character integration key into ``.env`` as
   ``AIOPS_PAGERDUTY_INTEGRATION_KEY=...``.
5. Add yourself to the on-call schedule on the service.

The dev server's ``_register_chatops_adapters`` hook reads the env var
and registers this adapter only when the key is set, so the demo runs
fine without PagerDuty configured.

Failure handling:
    Per-message failures log + raise. ``ChatOpsClient.send`` catches
    per-adapter exceptions so a PD outage / rate-limit / expired key can
    never block the JSONL audit log or the dashboard from receiving the
    same message.

Secret hygiene:
    The integration key is never logged or returned in responses.
    ``__repr__`` is overridden to redact the key so an accidental
    ``logger.info("%r", adapter)`` cannot leak it.

Dedup:
    Events API v2 auto-dedups on ``dedup_key``. We use ``incident_id``
    when RA-001/RA-003 has assigned one; otherwise fall back to a stable
    ``service:title`` hash. Re-firing the same alert in the dedup window
    updates the existing PD incident instead of creating a duplicate.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import httpx

from ..models import ChatMessage, Severity

logger = logging.getLogger(__name__)

# Routing actions that cause this adapter to fire. RA-005 today emits
# only "page_oncall" but the set is plural so v2 escalation actions
# (e.g. "page_backup", "page_manager") can be added without touching
# call-site logic.
PAGE_ACTIONS: frozenset[str] = frozenset({"page_oncall"})

# Severity → PagerDuty event severity. PD only knows four levels; we
# collapse our five-level scale onto theirs.
_PD_SEVERITY_BY_CHAT: dict[Severity, str] = {
    Severity.P0: "critical",
    Severity.P1: "critical",
    Severity.P2: "error",
    Severity.P3: "warning",
    Severity.INFO: "info",
}
_PD_DEFAULT_SEVERITY = "error"

_EVENTS_API_URL = "https://events.pagerduty.com/v2/enqueue"
_TIMEOUT = float(os.environ.get("AIOPS_PAGERDUTY_TIMEOUT", "5"))
_SOURCE = "adaptive-aiops/RA-005"


class PagerDutyAdapter:
    """POST a page-worthy ``ChatMessage`` to PagerDuty Events API v2.

    Non-page messages are no-ops at adapter level (no HTTP call, no log
    noise) so the audit log and dashboard remain the source of "what
    fired"; PD only sees what actually needs human escalation.
    """

    name = "pagerduty"

    def __init__(self, integration_key: str) -> None:
        if not integration_key or len(integration_key.strip()) < 8:
            # PD integration keys are 32 chars; reject obviously bad input
            # at construction so misconfiguration surfaces at startup, not
            # at the first Sev-1.
            raise ValueError(
                "PagerDutyAdapter requires a non-empty integration key "
                "(expect a 32-char Events API v2 key from the PD service)."
            )
        self._integration_key = integration_key.strip()

    def send(self, msg: ChatMessage) -> None:
        if not PAGE_ACTIONS.intersection(msg.actions):
            # Not a page-worthy message. Silent skip is the correct
            # behaviour: the routing decision said don't page.
            return
        payload = self._build_payload(msg)
        try:
            r = httpx.post(_EVENTS_API_URL, json=payload, timeout=_TIMEOUT)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            # Don't include the integration key. ChatOpsClient re-logs
            # with adapter context for debuggability.
            logger.error(
                "pagerduty adapter: enqueue failed for %r (severity=%s, service=%s): %s",
                msg.title,
                msg.severity.value,
                msg.service,
                exc,
            )
            raise

    def __repr__(self) -> str:
        return "PagerDutyAdapter(integration_key=***)"

    # ─── payload ─────────────────────────────────────────────────────────

    def _build_payload(self, msg: ChatMessage) -> dict[str, Any]:
        """Shape the Events API v2 ``enqueue`` request body.

        Reference: developer.pagerduty.com/api-reference/events-api-v2/
        Required fields: routing_key, event_action, payload.summary,
        payload.source, payload.severity.
        """
        pd_severity = _PD_SEVERITY_BY_CHAT.get(msg.severity, _PD_DEFAULT_SEVERITY)
        dedup_key = self._dedup_key(msg)

        custom_details: dict[str, Any] = {
            "channel": msg.channel,
            "chat_severity": str(msg.severity),
            "actions": list(msg.actions),
            "mentions": list(msg.mentions),
        }
        if msg.body:
            custom_details["body"] = msg.body
        if msg.incident_id:
            custom_details["incident_id"] = msg.incident_id

        return {
            "routing_key": self._integration_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {
                "summary": msg.title,
                "source": msg.service or _SOURCE,
                "severity": pd_severity,
                "component": msg.service or "unknown",
                "group": msg.channel,
                "class": "aiops-routing",
                "custom_details": custom_details,
            },
        }

    @staticmethod
    def _dedup_key(msg: ChatMessage) -> str:
        """Stable per-incident key so re-firing the same alert updates,
        not duplicates, the PD incident."""
        if msg.incident_id:
            # Real incident id from RA-003 — best-quality dedup.
            return f"aiops:incident:{msg.incident_id}"
        # Fallback: hash service + title. Same logical alert will collide;
        # genuinely different alerts on the same service still get
        # different titles → different keys.
        basis = f"{msg.service or 'unknown'}|{msg.title}"
        digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
        return f"aiops:hash:{digest}"
