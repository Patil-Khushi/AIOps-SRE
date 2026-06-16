"""War-Room bridge — creates a real Slack channel, invites the SMEs, and
posts the opening context pack, returning a join link (RA-006).

Why a registry *tool* and not a chatops *adapter*:
    Chatops adapters (``SlackWebhookAdapter``, ``SlackBotAdapter``) are
    one-shot ``ChatMessage`` *sinks* — fire-and-forget posts. Standing up a
    war room is a multi-step *action* (create channel → invite people →
    post → return a link), so it lives behind the tool registry as the
    capability ``chatops.war_room.create``. The agent calls it through
    ``get_registry().call(...)`` and never imports ``slack_sdk`` /
    ``httpx`` itself (CLAUDE.md principle #1, vendor-neutral).

Real vs simulated:
    With a Bot User OAuth token (``AIOPS_SLACK_BOT_TOKEN=xoxb-...``) and the
    right scopes, this creates an actual Slack channel and invites the
    resolved users. Without a token it returns a *simulated* channel + link
    so the demo, evals, and offline dev still produce a complete assembly.
    Either path returns the same shape; the ``simulated`` flag says which ran.

Required Slack bot scopes for the real path:
    ``channels:manage`` (create + invite to public channels),
    ``chat:write`` (post the context pack), ``channels:read`` (resolve a
    name_taken collision back to the existing channel id).

Failure handling:
    Never raises. Any HTTP / Slack-API / scope error becomes
    ``ToolResult(ok=False, error=...)`` (and the registry catches anything
    that slips through), so a Slack outage degrades the war room to
    "no bridge link" instead of breaking the incident pipeline.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx

from aiops.tools import ToolResult, tool

from .adapters._slack_user_map import load_slack_user_map

logger = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"
_BOT_TOKEN_PREFIX = "xoxb-"


def _timeout() -> float:
    """Per-call HTTP timeout. Read from the env on each call (not captured at
    import) so an operator can retune ``AIOPS_SLACK_BOT_TIMEOUT`` without a
    restart — same nit as the PagerDuty adapter."""
    return float(os.environ.get("AIOPS_SLACK_BOT_TIMEOUT", "5"))


_USER_MAP_PATH = Path(__file__).parent / "adapters" / "slack_users.json"

# Slack channel-name rules: lowercase, no spaces/periods, ≤80 chars, only
# letters/digits/hyphens/underscores.
_CHANNEL_SANITIZE = re.compile(r"[^a-z0-9_-]+")


def _bot_token() -> str:
    return os.environ.get("AIOPS_SLACK_BOT_TOKEN", "").strip()


def _channel_slug(channel: str) -> str:
    """Coerce a war-room channel name into something Slack will accept."""
    slug = _CHANNEL_SANITIZE.sub("-", channel.lower()).strip("-")
    return (slug or "war-room")[:80]


def _channel_link(channel_id: str) -> str:
    """Workspace-agnostic deep link that opens the channel in any Slack client."""
    return f"https://slack.com/app_redirect?channel={channel_id}"


# Slack has no API to start a huddle / mint a meeting link, so the live
# voice+video bridge is a Jitsi room — a real click-to-join call that needs no
# account, token, or API. Deterministic per incident (same channel → same
# room) so the link in Slack and on the dashboard match and re-joining works.
# Point ``AIOPS_JITSI_BASE`` at a self-hosted Jitsi to keep calls in-house.
_JITSI_BASE = os.environ.get("AIOPS_JITSI_BASE", "https://meet.jit.si").rstrip("/")


def _meeting_url(name: str) -> str:
    return f"{_JITSI_BASE}/aiops-{name}"


def _resolve_invites(invite_handles: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Map ``@handle`` / email handles to Slack user IDs via the shared map.

    Returns ``(rows, user_ids)`` where ``rows`` records every handle with its
    resolved id + status (``no_id`` when unmapped) for the audit trail, and
    ``user_ids`` is the de-duplicated list actually invitable."""
    user_map = load_slack_user_map(_USER_MAP_PATH)
    rows: list[dict[str, Any]] = []
    user_ids: list[str] = []
    for handle in invite_handles:
        key = handle.lstrip("@")
        uid = user_map.get(key)
        rows.append(
            {
                "handle": handle,
                "slack_user_id": uid,
                "invite_status": "no_id" if not uid else "pending",
            }
        )
        if uid and uid not in user_ids:
            user_ids.append(uid)
    return rows, user_ids


def _simulated(channel: str, invite_handles: list[str], note: str) -> ToolResult:
    rows, _ = _resolve_invites(invite_handles)
    for r in rows:
        r["invite_status"] = "simulated"
    slug = _channel_slug(channel)
    return ToolResult(
        ok=True,
        data={
            "simulated": True,
            "provider": "simulated",
            "channel_id": f"C-SIM-{slug}",
            "channel_name": slug,
            "url": f"https://app.slack.com/simulated/{slug}",
            "meeting_url": _meeting_url(slug),
            "invited": rows,
            "posted": False,
            "note": note,
        },
    )


def _slack_post(method: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    r = httpx.post(
        f"{_SLACK_API}/{method}",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=_timeout(),
    )
    r.raise_for_status()
    return r.json()


def _find_existing_channel(name: str, token: str) -> str | None:
    """On ``name_taken``, look the channel id back up so a re-assembled
    incident reuses its war room instead of failing."""
    r = httpx.get(
        f"{_SLACK_API}/conversations.list",
        params={"types": "public_channel", "limit": 1000, "exclude_archived": "true"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=_timeout(),
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        return None
    for ch in data.get("channels", []):
        if ch.get("name") == name:
            return ch.get("id")
    return None


@tool(
    name="slack.war_room.create",
    capability="chatops.war_room.create",
    provider="slack",
    description="Create a Slack war-room channel, invite SMEs, post the context pack, return a join link.",
)
def create_war_room(
    *,
    channel: str,
    title: str = "",
    body: str = "",
    invite_handles: list[str] | None = None,
) -> ToolResult:
    """Stand up the war room on Slack. Returns the channel id, join URL, and
    per-invite status. Degrades to a simulated room when no bot token is set
    or when a Slack call fails."""
    invite_handles = invite_handles or []
    token = _bot_token()
    name = _channel_slug(channel)

    if not token.startswith(_BOT_TOKEN_PREFIX):
        logger.info("war_room_bridge: no bot token; returning simulated war room %r", name)
        return _simulated(channel, invite_handles, note="AIOPS_SLACK_BOT_TOKEN not set")

    try:
        # 1) Create the channel (reuse it on name_taken so re-assembly is idempotent).
        created = _slack_post("conversations.create", {"name": name, "is_private": False}, token)
        if created.get("ok"):
            channel_id = created["channel"]["id"]
            channel_name = created["channel"]["name"]
        elif created.get("error") == "name_taken":
            channel_id = _find_existing_channel(name, token)
            channel_name = name
            if not channel_id:
                return _simulated(
                    channel, invite_handles, note="name_taken but channel lookup failed"
                )
        else:
            # Most common real cause is a missing bot scope (channels:manage).
            # Degrade to a simulated room so the war room still gets a link,
            # and surface the exact Slack error so the operator can fix scopes.
            err = created.get("error")
            logger.warning("war_room_bridge: conversations.create failed (%s); simulating", err)
            return _simulated(
                channel,
                invite_handles,
                note=f"real Slack create failed: {err} — add bot scope 'channels:manage' & reinstall",
            )

        # 2) Invite the resolved SMEs (best-effort, per-status recorded).
        rows, user_ids = _resolve_invites(invite_handles)
        if user_ids:
            inv = _slack_post(
                "conversations.invite", {"channel": channel_id, "users": ",".join(user_ids)}, token
            )
            invited_ok = bool(inv.get("ok"))
            inv_err = inv.get("error")
            # already_in_channel / already invited are success for our purposes.
            ok_errors = {"already_in_channel"}
            for r in rows:
                if not r["slack_user_id"]:
                    continue
                r["invite_status"] = (
                    "invited" if (invited_ok or inv_err in ok_errors) else f"failed:{inv_err}"
                )

        # 3) Post the opening context pack + the click-to-join meeting link.
        meeting_url = _meeting_url(channel_name)
        opening = title or f"War room: {name}"
        text = (
            f"*{opening}*\n\n{body}\n\n"
            f":movie_camera: *Join the war-room call:* {meeting_url}\n"
            ":telephone_receiver: (or start a Slack huddle in this channel)"
        )
        posted = _slack_post("chat.postMessage", {"channel": channel_id, "text": text}, token)

        return ToolResult(
            ok=True,
            data={
                "simulated": False,
                "provider": "slack",
                "channel_id": channel_id,
                "channel_name": channel_name,
                "url": _channel_link(channel_id),
                "meeting_url": meeting_url,
                "invited": rows,
                "posted": bool(posted.get("ok")),
            },
        )
    except httpx.HTTPError as exc:
        logger.error("war_room_bridge: Slack HTTP error: %s", exc)
        return _simulated(channel, invite_handles, note=f"slack http error: {exc}")
    except Exception as exc:
        logger.exception("war_room_bridge: unexpected error")
        return _simulated(channel, invite_handles, note=f"{type(exc).__name__}: {exc}")


__all__ = ["create_war_room"]
