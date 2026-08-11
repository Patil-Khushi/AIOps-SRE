"""Teams war-room meeting — creates a real calendar meeting and invites the SMEs.

Registered as the capability ``chatops.war_room.meeting.create`` so the
war-room assembler asks for "a live bridge" and never learns whether it
came from Teams, Jitsi, or nothing at all (CLAUDE.md principle #1).

How it works
------------
POSTs the incident to a Power Automate flow whose single action is the
Teams connector's ``CreateTeamsMeeting``. Attendees receive a genuine
Outlook/Teams invite with response tracking, and the event carries a
``teams.microsoft.com/l/meetup-join/...`` URL.

Why the flow rather than Graph
------------------------------
``POST /me/onlineMeetings`` needs an Azure AD app registration with
``OnlineMeetings.ReadWrite.All``, admin consent, and a Teams application
access policy — all of which require a tenant admin. The flow reuses the
Teams connection that already exists for chat notifications, so this needs
no new consent. The organiser is whoever owns that connection.

The async round-trip
--------------------
The webhook trigger is fire-and-forget (HTTP 202), so the join URL is not
in the response — it is read back from the flow's run history. Measured at
~3s end to end, which is why this can sit inline in war-room assembly. The
wait is bounded by ``AIOPS_TEAMS_MEETING_WAIT_SECONDS``; on timeout the
meeting has still been created and the invites still went out, so the
caller degrades to "check your calendar" rather than losing the war room.

Failure handling
----------------
Never raises. Every failure path returns ``ToolResult(ok=False, ...)`` so a
Power Automate outage costs the incident its bridge link, not its page.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from aiops.tools import ToolResult, tool

logger = logging.getLogger(__name__)

_FLOW_API = "https://api.flow.microsoft.com"
_API_VERSION = "2016-11-01"


def _webhook_url() -> str:
    return os.environ.get("AIOPS_TEAMS_MEETING_WEBHOOK_URL", "").strip()


def _wait_budget() -> float:
    """Seconds to wait for the join URL. Read per call so an operator can
    retune without a restart."""
    return float(os.environ.get("AIOPS_TEAMS_MEETING_WAIT_SECONDS", "10"))


def _duration_minutes() -> int:
    return int(os.environ.get("AIOPS_TEAMS_MEETING_MINUTES", "30"))


def _timeout() -> float:
    return float(os.environ.get("AIOPS_TEAMS_MEETING_TIMEOUT", "15"))


def _az_token() -> str | None:
    """Token for reading the flow's run history. Optional: without it the
    meeting is still created, we just cannot read the join URL back."""
    az = os.environ.get("AIOPS_AZ_PATH", r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd")
    try:
        out = subprocess.run(
            [
                az,
                "account",
                "get-access-token",
                "--resource",
                "https://service.flow.microsoft.com/",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(out.stdout)["accessToken"]
    except Exception as exc:
        logger.debug("teams_meeting: no flow-API token (%s); join URL unavailable", exc)
        return None


def _output_body(action: dict[str, Any]) -> dict[str, Any]:
    """Action outputs arrive wrapped in a {statusCode, headers, body} envelope
    behind a short-lived pre-signed URL."""
    uri = (action.get("properties", {}).get("outputsLink") or {}).get("uri")
    if not uri:
        return {}
    try:
        payload = httpx.get(uri, timeout=_timeout()).json()
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    body = payload.get("body", payload)
    return body if isinstance(body, dict) else {}


def _poll_join_url(started_at: float) -> tuple[str | None, str | None]:
    """Read the newest run's join URL. Returns ``(join_url, note)``."""
    env_id = os.environ.get("AIOPS_POWER_AUTOMATE_ENV", "").strip()
    flow_id = os.environ.get("AIOPS_TEAMS_MEETING_FLOW_ID", "").strip()
    if not env_id or not flow_id:
        return None, "AIOPS_POWER_AUTOMATE_ENV / AIOPS_TEAMS_MEETING_FLOW_ID not set"
    token = _az_token()
    if not token:
        return None, "no Power Automate token; could not read the join URL"

    headers = {"Authorization": f"Bearer {token}"}
    deadline = started_at + _wait_budget()
    runs_url = (
        f"{_FLOW_API}/providers/Microsoft.ProcessSimple/environments/{env_id}/flows/{flow_id}/runs"
    )
    try:
        with httpx.Client(headers=headers, timeout=_timeout()) as c:
            while time.monotonic() < deadline:
                time.sleep(1.0)
                r = c.get(runs_url, params={"api-version": _API_VERSION, "$top": 1})
                if r.status_code >= 400:
                    continue
                runs = r.json().get("value") or []
                if not runs:
                    continue
                run = runs[0]
                status = run["properties"].get("status")
                if status not in ("Succeeded", "Failed"):
                    continue
                if status == "Failed":
                    return None, "meeting flow run failed"
                ra = c.get(
                    f"{runs_url}/{run['name']}/actions", params={"api-version": _API_VERSION}
                )
                if ra.status_code >= 400:
                    return None, "could not read meeting flow actions"
                for action in ra.json().get("value", []):
                    body = _output_body(action)
                    join = (body.get("onlineMeeting") or {}).get("joinUrl")
                    if join:
                        return join, None
                return None, "meeting created but no join URL in the run output"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return None, "timed out waiting for the join URL"


@tool(
    name="teams.war_room.meeting.create",
    capability="chatops.war_room.meeting.create",
    provider="teams",
    description="Create a Teams war-room meeting and invite the SMEs by email; returns the join URL.",
)
def create_meeting(
    *,
    subject: str,
    attendee_emails: list[str] | None = None,
    body_html: str = "",
    incident_id: str | None = None,
) -> ToolResult:
    """Schedule the war-room bridge. ``attendee_emails`` get real invites.

    Returns ``meeting_url`` when the join link could be read back, and
    ``invited`` echoing who was actually put on the invite.
    """
    url = _webhook_url()
    if not url:
        return ToolResult(
            ok=False,
            error="AIOPS_TEAMS_MEETING_WEBHOOK_URL not set",
            metadata={"provider": "teams"},
        )

    # De-duplicate while preserving order, and drop anything that isn't an
    # address — the connector rejects the whole event on a malformed
    # attendee, which would cost us the meeting entirely.
    emails: list[str] = []
    for raw in attendee_emails or []:
        e = (raw or "").strip()
        if e and "@" in e and e.lower() not in {x.lower() for x in emails}:
            emails.append(e)
    if not emails:
        return ToolResult(
            ok=False,
            error="no attendee emails resolved; not creating an empty meeting",
            metadata={"provider": "teams"},
        )

    start = datetime.now(UTC)
    end = start + timedelta(minutes=_duration_minutes())
    payload = {
        "subject": subject,
        "body_html": body_html or subject,
        # The connector takes a single semicolon-delimited string.
        "attendees": ";".join(emails),
        "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "time_zone": "UTC",
        "incident_id": incident_id or "",
    }

    started_at = time.monotonic()
    try:
        r = httpx.post(url, json=payload, timeout=_timeout())
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # str() of an HTTPStatusError embeds the signed URL; log the status only.
        status = exc.response.status_code
        logger.error("teams_meeting: flow trigger failed: HTTP %s", status)
        return ToolResult(
            ok=False,
            error=f"meeting flow POST failed: HTTP {status}",
            metadata={"provider": "teams"},
        )
    except Exception as exc:
        logger.error("teams_meeting: flow trigger failed: %s", type(exc).__name__)
        return ToolResult(
            ok=False,
            error=f"meeting flow POST failed: {type(exc).__name__}",
            metadata={"provider": "teams"},
        )

    join_url, note = _poll_join_url(started_at)
    return ToolResult(
        ok=True,
        data={
            "provider": "teams",
            "meeting_url": join_url,
            "invited": emails,
            "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
            # Truthful even when the link is missing: the invite went out.
            "note": note or "",
        },
        metadata={"provider": "teams"},
    )


__all__ = ["create_meeting"]
