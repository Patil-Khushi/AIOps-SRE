"""Chatops seam — vendor-neutral notification routing.

Public API:

- ``ChatMessage`` / ``Severity`` — the canonical message ticket
- ``ChatOpsAdapter`` — protocol every sink implements
- ``ChatOpsClient`` — fans messages to all registered sinks
- ``get_client()`` — process-wide singleton accessor

D2 (WebSocket → React panel) and D3 (JSON audit log) plug their adapters
into ``get_client().register(...)`` at startup. Slack / Teams / PagerDuty
adapters land later without touching agent code.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .adapters.jsonfile import JsonFileChatOpsAdapter
from .adapters.pagerduty import PagerDutyAdapter
from .adapters.slack import SlackWebhookAdapter
from .client import ChatOpsAdapter, ChatOpsClient, DeliveryResult, get_client
from .models import ChatMessage, InteractivePrompt, Severity, to_record

__all__ = [
    "ChatMessage",
    "ChatOpsAdapter",
    "ChatOpsClient",
    "DeliveryResult",
    "InteractivePrompt",
    "Severity",
    "get_client",
    "to_record",
    "register_env_adapters",
]


def register_env_adapters(
    *,
    audit_path: str | Path,
    slack_webhook_url: str | None = None,
    pagerduty_integration_key: str | None = None,
) -> None:
    """Register the process-wide chatops sinks configured from the env.

    The demo server and other hosts can call this once at startup to wire
    the JSON audit log, Slack webhook, and PagerDuty adapter without
    duplicating the env-read logic.
    """

    client = get_client()
    registered_kinds = {type(adapter) for adapter in client.adapters}
    logger = logging.getLogger(__name__)

    if JsonFileChatOpsAdapter not in registered_kinds:
        client.register(JsonFileChatOpsAdapter(audit_path))

    slack_webhook_url = (
        slack_webhook_url
        if slack_webhook_url is not None
        else os.environ.get("AIOPS_SLACK_WEBHOOK_URL", "").strip()
    )
    if slack_webhook_url and SlackWebhookAdapter not in registered_kinds:
        try:
            client.register(SlackWebhookAdapter(slack_webhook_url))
        except ValueError as exc:
            logger.warning(
                "chatops: AIOPS_SLACK_WEBHOOK_URL set but invalid (%s); skipping",
                exc,
            )

    pagerduty_integration_key = (
        pagerduty_integration_key
        if pagerduty_integration_key is not None
        else os.environ.get("AIOPS_PAGERDUTY_INTEGRATION_KEY", "").strip()
    )
    if pagerduty_integration_key and PagerDutyAdapter not in registered_kinds:
        try:
            client.register(PagerDutyAdapter(pagerduty_integration_key))
        except ValueError as exc:
            logger.warning(
                "chatops: AIOPS_PAGERDUTY_INTEGRATION_KEY set but invalid (%s); skipping",
                exc,
            )
