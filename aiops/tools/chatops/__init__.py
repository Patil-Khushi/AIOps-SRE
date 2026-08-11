"""Chatops seam — vendor-neutral notification routing.

Public API:

- ``ChatMessage`` / ``Severity`` — the canonical message ticket
- ``ChatOpsAdapter`` — protocol every sink implements
- ``ChatOpsClient`` — fans messages to all registered sinks
- ``get_client()`` — process-wide singleton accessor

D2 (WebSocket → React panel) and D3 (JSON audit log) plug their adapters
into ``get_client().register(...)`` at startup. Slack, Teams, and
PagerDuty adapters register from the env (``register_env_adapters``) —
none of them ever touched agent code, which is the point of the seam.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

# Registers the ``chatops.war_room.create`` and
# ``chatops.war_room.meeting.create`` capabilities (RA-006) via @tool on
# import, so they're available in every context the chatops seam is (server,
# CLI, evals) without each host wiring them explicitly.
from . import (
    teams_meeting,  # noqa: F401
    war_room_bridge,  # noqa: F401
)
from .adapters.jsonfile import JsonFileChatOpsAdapter
from .adapters.pagerduty import PagerDutyAdapter
from .adapters.slack import SlackWebhookAdapter
from .adapters.slack_bot import SlackBotAdapter
from .adapters.teams import TeamsWebhookAdapter
from .adapters.teams_dm import TeamsDmAdapter
from .client import ChatOpsAdapter, ChatOpsClient, DeliveryResult, get_client
from .models import ChatMessage, InteractivePrompt, Severity, to_record

logger = logging.getLogger(__name__)

__all__ = [
    "ChatMessage",
    "ChatOpsAdapter",
    "ChatOpsClient",
    "DeliveryResult",
    "InteractivePrompt",
    "JsonFileChatOpsAdapter",
    "PagerDutyAdapter",
    "Severity",
    "SlackBotAdapter",
    "SlackWebhookAdapter",
    "TeamsDmAdapter",
    "TeamsWebhookAdapter",
    "get_client",
    "register_env_adapters",
    "to_record",
]


def register_env_adapters(
    *,
    audit_path: str | Path,
    slack_webhook_url: str | None = None,
    pagerduty_integration_key: str | None = None,
    slack_bot_token: str | None = None,
    teams_webhook_url: str | None = None,
    teams_dm_webhook_url: str | None = None,
) -> None:
    """Register the process-wide chatops sinks configured from the env.

    The demo server and other hosts can call this once at startup to wire
    the JSON audit log, Slack webhook/bot, Teams webhook/DM, and PagerDuty
    adapter without duplicating the env-read logic.
    """

    client = get_client()
    registered_kinds = {type(adapter) for adapter in client.adapters}

    if JsonFileChatOpsAdapter not in registered_kinds:
        client.register(JsonFileChatOpsAdapter(audit_path))
        logger.info("chatops: registered jsonfile adapter -> %s", audit_path)

    slack_webhook_url = (
        slack_webhook_url
        if slack_webhook_url is not None
        else os.environ.get("AIOPS_SLACK_WEBHOOK_URL", "").strip()
    )
    if slack_webhook_url and SlackWebhookAdapter not in registered_kinds:
        try:
            client.register(SlackWebhookAdapter(slack_webhook_url))
            logger.info("chatops: registered slack webhook adapter")
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
            logger.info("chatops: registered pagerduty adapter (page_oncall actions only)")
        except ValueError as exc:
            logger.warning(
                "chatops: AIOPS_PAGERDUTY_INTEGRATION_KEY set but invalid (%s); skipping",
                exc,
            )

    slack_bot_token = (
        slack_bot_token
        if slack_bot_token is not None
        else os.environ.get("AIOPS_SLACK_BOT_TOKEN", "").strip()
    )
    if slack_bot_token and SlackBotAdapter not in registered_kinds:
        try:
            client.register(SlackBotAdapter(slack_bot_token))
            logger.info("chatops: registered slack bot adapter (DMs on page_oncall actions only)")
        except ValueError as exc:
            logger.warning(
                "chatops: AIOPS_SLACK_BOT_TOKEN set but invalid (%s); skipping",
                exc,
            )

    teams_webhook_url = (
        teams_webhook_url
        if teams_webhook_url is not None
        else os.environ.get("AIOPS_TEAMS_WEBHOOK_URL", "").strip()
    )
    if teams_webhook_url and TeamsWebhookAdapter not in registered_kinds:
        try:
            client.register(TeamsWebhookAdapter(teams_webhook_url))
            logger.info("chatops: registered teams webhook adapter")
        except ValueError as exc:
            logger.warning(
                "chatops: AIOPS_TEAMS_WEBHOOK_URL set but invalid (%s); skipping",
                exc,
            )

    teams_dm_webhook_url = (
        teams_dm_webhook_url
        if teams_dm_webhook_url is not None
        else os.environ.get("AIOPS_TEAMS_DM_WEBHOOK_URL", "").strip()
    )
    if teams_dm_webhook_url and TeamsDmAdapter not in registered_kinds:
        try:
            client.register(TeamsDmAdapter(teams_dm_webhook_url))
            logger.info(
                "chatops: registered teams dm adapter (page/notify with assignee email only)"
            )
        except ValueError as exc:
            logger.warning(
                "chatops: AIOPS_TEAMS_DM_WEBHOOK_URL set but invalid (%s); skipping",
                exc,
            )
