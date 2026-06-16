"""DB-backed provider for capability ``oncall.schedule.lookup``.

Replaces the mock at ``aiops/tools/mock_providers.py:mock_oncall_lookup``
with a real shift-aware lookup against ``aiops/state/oncall_repository``.

The wire shape is a *superset* of the mock's so existing agents
(RA-001 Alert Triage, RA-002 Incident Classifier) keep working without
changes. New fields (``slack_handle``, ``slack_user_id``, ``name``,
``skills``, ``role``, ``team``) are additive — RA-005 reads them to
produce real Slack pings; the older agents ignore them.

Registration happens at import time via the ``@tool`` decorator. The
demo server activates this provider by calling
``get_registry().select_provider("oncall.schedule.lookup",
"db.oncall.schedule.lookup")`` at startup; tests can opt back to the
mock the same way.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiops.state.oncall_repository import (
    OnCallEngineer,
    find_best_for_team_and_category,
    find_oncall_for_team,
)

from .registry import ToolResult, tool

logger = logging.getLogger(__name__)


def _engineer_to_data(eng: OnCallEngineer | None, team: str) -> dict:
    """Shape the lookup result for callers.

    Always returns the ``engineer_email`` key for backward compatibility
    with the mock (None when no on-call found). New fields are present
    too; callers that don't need them simply ignore them.
    """
    if eng is None:
        return {
            "team": team,
            "engineer_email": None,
            "engineer_name": None,
            "slack_handle": None,
            "slack_user_id": None,
            "role": None,
            "skills": [],
            "timezone": None,
            "matched_category": None,
            "matched_category_display": None,
            "via_wildcard": False,
        }
    return {
        "team": eng.team,
        "engineer_email": eng.email,
        "engineer_name": eng.name,
        "slack_handle": eng.slack_handle,
        "slack_user_id": eng.slack_user_id,
        "role": eng.role,
        "skills": list(eng.skills),
        "timezone": eng.timezone,
        "matched_category": eng.matched_category,
        "matched_category_display": eng.matched_category_display,
        "via_wildcard": eng.via_wildcard,
    }


@tool(
    name="db.oncall.schedule.lookup",
    capability="oncall.schedule.lookup",
    provider="db",
    description="Look up the engineer currently on-call for a team (shift + skill + category-expertise aware).",
)
def db_oncall_lookup(
    team: str,
    required_skills: list[str] | None = None,
    category_keywords: list[str] | None = None,
    service: str | None = None,
) -> ToolResult:
    """Resolve the engineer who should be paged for ``team`` right now.

    Three optional refinements layer on top of the basic shift lookup:

    * ``required_skills`` — engineers tagged with all of them are
      preferred, with a graceful fallback to "anyone on shift" if no
      strict match exists.
    * ``category_keywords`` — terms extracted from the alert (service
      name, metric name, error kind, summary keywords). When provided,
      we route to the engineer with the strongest expertise score in
      any matched failure category (e.g. ``payment-gateway`` vs
      ``payment-database`` within the Payments Team). Falls back to the
      plain shift lookup if nothing matches.
    * ``service`` — the alert's affected service. Enables sticky
      assignment: a service with an incident assigned within the sticky
      window re-pages the same engineer (if still active + on shift)
      instead of rotating — same incident, same person.

    See ``aiops/state/oncall_repository`` for the full fallback ladder.
    """
    now = datetime.now(UTC)
    keywords = [k for k in (category_keywords or []) if k and k.strip()]
    try:
        if keywords:
            engineer = find_best_for_team_and_category(
                team,
                keywords,
                now=now,
                required_skills=list(required_skills or []),
                service=service,
            )
        else:
            engineer = find_oncall_for_team(
                team,
                now=now,
                required_skills=list(required_skills or []),
                service=service,
            )
    except Exception as exc:  # boundary: never let a DB blip break triage
        logger.exception("oncall.schedule.lookup: DB lookup failed for team=%r", team)
        return ToolResult(
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            metadata={"provider": "db", "team": team},
        )

    data = _engineer_to_data(engineer, team)
    return ToolResult(
        ok=True,
        data=data,
        metadata={
            "provider": "db",
            "matched": engineer is not None,
            "lookup_at": now.isoformat(),
            "category_keywords": keywords,
        },
    )


__all__ = ["db_oncall_lookup"]
