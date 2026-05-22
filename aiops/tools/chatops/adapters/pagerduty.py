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

Defence-in-depth on severity (addresses CR #3 on PR #96):
    Even when ``page_oncall`` is present, we refuse to page on severities
    below P2. A bug in RA-005 that incorrectly appends ``page_oncall`` to
    a Sev-4 noise message would otherwise wake on-call at 3 AM. The cost
    of a false page is high enough that the redundant check earns its keep.

Non-blocking delivery (addresses CR #1 on PR #96):
    The HTTP POST to PagerDuty runs on a daemon background thread, so
    ``send()`` returns immediately. This is the correct semantic for paging:
    the caller (the API request) should not block on PD round-trips, and a
    failed page is logged but never propagated back to the request that
    triggered the alert in the first place. The seam-level fix (making
    ``ChatOpsClient`` async-aware so every adapter can be properly
    non-blocking) is tracked separately — see PR #96 review and the
    follow-up issue this PR's reviewer asked for.

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

Secret hygiene:
    The integration key is never logged or returned in responses.
    ``__repr__`` is overridden to redact the key so an accidental
    ``logger.info("%r", adapter)`` cannot leak it.

PII note:
    ``custom_details.mentions`` may contain engineer emails (e.g.
    ``oncall@payments.example.com``). PagerDuty already stores on-call
    user identities for the service, so blast radius is bounded to the
    PD tenant, but operators should know these strings leave our process.

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
import re
import threading
import time
from typing import Any

import httpx

from ..models import ChatMessage, Severity

logger = logging.getLogger(__name__)

# Routing actions that cause this adapter to fire. RA-005 today emits
# only "page_oncall" but the set is plural so v2 escalation actions
# (e.g. "page_backup", "page_manager") can be added without touching
# call-site logic.
PAGE_ACTIONS: frozenset[str] = frozenset({"page_oncall"})

# Severities for which we'll honour a page_oncall action. P3 / INFO with
# page_oncall is a contradiction that almost always indicates a routing
# bug upstream — refuse rather than wake someone.
PAGE_WORTHY_SEVERITIES: frozenset[Severity] = frozenset({Severity.P0, Severity.P1, Severity.P2})

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
_DEFAULT_TIMEOUT = 5.0
_SOURCE = "adaptive-aiops/RA-005"

# One short retry on transient HTTP failures (DNS blip, connection reset,
# PD enqueue 5xx during deploy). Page-worthy alerts are by definition
# expensive to drop; a single retry recovers most "just had a network
# burp" cases at ~0.5s extra latency on the daemon thread. Doesn't help
# the "process killed mid-flight" failure mode — that needs graceful
# shutdown, tracked separately (follow-up issue).
_RETRY_COUNT = 1
_RETRY_BACKOFF_SECONDS = 0.5

# PagerDuty Events API v2 integration keys are exactly 32 alphanumeric
# characters. A regex check at construction surfaces copy-paste errors
# (truncation, accidentally pasting "API_KEY" placeholder text, leading
# whitespace, etc.) at server startup rather than at the first Sev-1.
_KEY_PATTERN = re.compile(r"[A-Za-z0-9]{32}")


class PagerDutyAdapter:
    """POST a page-worthy ``ChatMessage`` to PagerDuty Events API v2.

    Non-page messages are no-ops at adapter level (no HTTP call, no log
    noise) so the audit log and dashboard remain the source of "what
    fired"; PD only sees what actually needs human escalation.
    """

    name = "pagerduty"

    def __init__(
        self,
        integration_key: str,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        cleaned = (integration_key or "").strip()
        if not _KEY_PATTERN.fullmatch(cleaned):
            raise ValueError(
                "PagerDutyAdapter requires a 32-character alphanumeric Events API v2 "
                "integration key. The key from the PD service's Integrations tab is "
                "the right shape; common mistakes are pasting placeholder text, "
                "truncating, or copying the service id instead of the integration key."
            )
        self._integration_key = cleaned
        # Read timeout per-instance (CR nice-to-have): keeps it consistent
        # with adapters that pick up env at construction, and lets tests
        # inject a different timeout cleanly.
        self._timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("AIOPS_PAGERDUTY_TIMEOUT", _DEFAULT_TIMEOUT)
        )

    def send(self, msg: ChatMessage) -> None:
        if not PAGE_ACTIONS.intersection(msg.actions):
            # Not a page-worthy message. Silent skip is the correct
            # behaviour: the routing decision said don't page.
            return
        if msg.severity not in PAGE_WORTHY_SEVERITIES:
            # Defence in depth: the actions list says "page" but the
            # severity disagrees. Refuse rather than wake someone on a
            # contradiction. Log loudly so the upstream routing bug is
            # surfaced.
            logger.warning(
                "pagerduty: refusing to page on severity=%s for %r "
                "(actions=%r). RA-005 should not emit page_oncall below P2; "
                "this indicates an upstream routing bug.",
                msg.severity.value,
                msg.title,
                list(msg.actions),
            )
            return
        payload = self._build_payload(msg)
        # Fire-and-forget on a daemon thread: paging must not block the
        # API request that produced the alert, and a slow / unreachable
        # PD must not back-pressure the chatops seam.
        threading.Thread(
            target=self._post,
            args=(payload, msg.title, msg.severity.value, msg.service),
            name=f"pagerduty-{msg.severity.value}",
            daemon=True,
        ).start()

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

    def _post(
        self,
        payload: dict[str, Any],
        title: str,
        severity: str,
        service: str | None,
    ) -> None:
        """Daemon-thread target: do the actual HTTP POST + log failures.

        Exceptions are logged but never raised — they have no live caller
        to propagate to (we're on a background thread). The audit log +
        WebSocket dashboard already reflect "the system intended to page";
        PD-side failures show up in PD's own incident-status visibility.

        One retry with a short backoff covers the common transient-fail
        case (DNS blip, connection reset, PD 5xx during deploy) without
        turning this into a real retry queue. The "process killed
        mid-flight" failure mode is out of scope here — graceful shutdown
        belongs with the async-seam refactor.
        """
        last_exc: httpx.HTTPError | None = None
        for attempt in range(_RETRY_COUNT + 1):
            try:
                r = httpx.post(_EVENTS_API_URL, json=payload, timeout=self._timeout)
                r.raise_for_status()
                return
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < _RETRY_COUNT:
                    time.sleep(_RETRY_BACKOFF_SECONDS)
        logger.error(
            "pagerduty adapter: enqueue failed for %r (severity=%s, service=%s) "
            "after %d attempt(s): %s",
            title,
            severity,
            service,
            _RETRY_COUNT + 1,
            last_exc,
        )

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
