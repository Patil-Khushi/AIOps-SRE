"""On-call DB queries — backs the ``oncall.schedule.lookup`` tool provider.

The single entry point is :func:`find_oncall_for_team`. It picks the
engineer who should be paged for ``team`` at ``now``, applying the same
fallback ladder a human dispatcher would:

1. **Primary** on shift right now → return.
2. If none, **secondary** on shift right now → return.
3. If none, the ``manager_escalation`` row for the team (no shift filter,
   that role is always-on).
4. If none, ``None``.

Optional ``required_skills`` filters candidates to engineers tagged with
ALL of those skills. Falls back to no-skill match if the strict match
finds nobody — better to wake someone unskilled than to wake nobody on
a real Sev-1.

The repository is import-side-effect-free and uses its own Session
context — callers don't pass a session in. Keeps the seam clean.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from aiops.state import get_engine
from aiops.state.models import (
    EngineerExpertiseRow,
    EngineerRow,
    FailureCategoryRow,
    ShiftRow,
)

logger = logging.getLogger(__name__)

# Order matters: primary takes precedence; manager_escalation is the
# always-on safety net.
_ROLE_FALLBACK = ("primary", "secondary", "manager_escalation")

# Special ``ShiftRow.team`` value that signals "covers ANY team that has
# no other coverage". When the team-specific ladder yields nobody, the
# repository searches for rows tagged with this value and returns the
# matching ``manager_escalation`` engineer. This is the
# never-drop-an-alert rung: a Sev-1 on a service whose team isn't even
# in the on-call DB still pages the platform-wide escalation rather
# than silently dropping the page.
_GLOBAL_TEAM_KEY = "*"

# Proficiency level → base score. Tuned so expert dominates intermediate
# but not by so much that an intermediate with strong feedback + many
# resolved incidents can't beat a fresh expert.
_PROFICIENCY_WEIGHTS: dict[str, float] = {
    "novice": 10.0,
    "intermediate": 50.0,
    "expert": 100.0,
    "principal": 150.0,
}
# Cap incidents_resolved when scoring so a single veteran with 200 fixes
# doesn't always win against a strong-feedback expert with 10. Above this
# threshold extra incidents stop adding to the score.
_INCIDENTS_CAP = 25


@dataclass(frozen=True)
class OnCallEngineer:
    """Plain-data result of an on-call lookup.

    Frozen so callers can't accidentally mutate it mid-handler. All the
    fields the chatops seam + Slack adapter need are here so RA-005 (and
    any future agent) doesn't need a second round-trip to the DB.

    ``matched_category`` / ``matched_category_display`` are set only by
    :func:`find_best_for_team_and_category` — they tell the caller *which*
    failure sub-domain drove the pick (e.g. ``payment-gateway`` /
    "Payment Gateway"). Plain shift lookup leaves them ``None``.
    """

    id: int
    name: str
    email: str
    slack_handle: str | None
    slack_user_id: str | None
    team: str
    role: str  # which fallback bucket they came from
    skills: list[str]
    timezone: str
    matched_category: str | None = None
    matched_category_display: str | None = None


def _shift_covers_now(shift: ShiftRow, now: datetime) -> bool:
    """Return True if ``shift`` is active at ``now`` (UTC).

    ``manager_escalation`` rows are treated as always-on regardless of
    the stored hours, because that role is the safety net of last resort.
    """
    if shift.role == "manager_escalation":
        return True
    if shift.day_of_week != now.weekday():
        return False
    # Inclusive start, exclusive end — same convention as Python ranges.
    return shift.start_hour_utc <= now.hour < shift.end_hour_utc


def _engineer_has_skills(engineer: EngineerRow, required: list[str]) -> bool:
    if not required:
        return True
    have = set(engineer.skills)
    return all(s in have for s in required)


def _find_global_escalation(
    requested_team: str,
    *,
    now: datetime,
    required_skills: list[str],
) -> OnCallEngineer | None:
    """Look up the platform-wide ``manager_escalation`` engineer.

    Backs the *never-drop-an-alert* rung. Returns the engineer registered
    against ``ShiftRow.team == "*"`` with role ``manager_escalation``.
    The returned :class:`OnCallEngineer` keeps ``team`` set to the
    caller's ``requested_team`` so the routing context (channel name,
    audit trail, "On-call for which team?") stays honest even though
    the engineer's primary team may differ — the role string
    (``manager_escalation``) is the signal that this is the safety-net
    rung, not the team owner.

    Skill filtering is honoured but falls back to "anyone wildcard on
    shift" if no skill match — same anti-fatigue heuristic as
    :func:`find_oncall_for_team`.
    """
    with Session(get_engine()) as session:
        rows = session.exec(
            select(ShiftRow, EngineerRow)
            .join(EngineerRow, ShiftRow.engineer_id == EngineerRow.id)
            .where(ShiftRow.team == _GLOBAL_TEAM_KEY)
            .where(ShiftRow.role == "manager_escalation")
            .where(EngineerRow.active.is_(True))  # type: ignore[attr-defined]
        ).all()

    candidates = [(s, e) for s, e in rows if _engineer_has_skills(e, required_skills)]
    if not candidates and required_skills:
        candidates = list(rows)
    if not candidates:
        return None

    # Deterministic pick: lowest engineer id wins (same proxy as the
    # team-specific path uses).
    _shift, chosen = min(candidates, key=lambda pair: pair[1].id or 0)
    logger.info(
        "oncall(global): team=%r had no coverage -> wildcard engineer=%r",
        requested_team,
        chosen.name,
    )
    return OnCallEngineer(
        id=chosen.id or -1,
        name=chosen.name,
        email=chosen.email,
        slack_handle=chosen.slack_handle,
        slack_user_id=chosen.slack_user_id,
        team=requested_team,
        role="manager_escalation",
        skills=chosen.skills,
        timezone=chosen.timezone,
    )


def find_oncall_for_team(
    team: str,
    *,
    now: datetime,
    required_skills: list[str] | None = None,
) -> OnCallEngineer | None:
    """Look up the on-call engineer for ``team`` at ``now``.

    Lookup ladder, top-down:

    1. **Team-specific roles** (primary → secondary → manager_escalation):
       each bucket is filtered by ``required_skills`` first, then re-tried
       without skill filtering — "wake someone unskilled" beats "wake
       nobody on a Sev-1."
    2. **Global wildcard escalation** (``ShiftRow.team == "*"``,
       role ``manager_escalation``): the never-drop rung. Engaged when
       the team isn't onboarded in the DB or its full ladder is off-shift.
       Common for OTel-demo services whose CMDB team has no engineers
       seeded (Catalog Team, Ads Team, etc.) — without this rung, a
       Sev-1 on those services would silently page nobody.

    Returns ``None`` only when *both* the team-specific ladder AND the
    wildcard rung are empty — e.g. the DB has been wiped and not
    re-seeded.
    """
    required = list(required_skills or [])

    with Session(get_engine()) as session:
        # One round-trip: pull all candidates for this team + their shifts.
        # POC scale is fine; if engineer count crosses 500 add an index hint.
        rows = session.exec(
            select(ShiftRow, EngineerRow)
            .join(EngineerRow, ShiftRow.engineer_id == EngineerRow.id)
            .where(ShiftRow.team == team)
            .where(EngineerRow.active.is_(True))  # type: ignore[attr-defined]
        ).all()

    # Group by role for the fallback walk.
    by_role: dict[str, list[tuple[ShiftRow, EngineerRow]]] = {r: [] for r in _ROLE_FALLBACK}
    for shift, eng in rows:
        bucket = by_role.get(shift.role)
        if bucket is not None:
            bucket.append((shift, eng))

    for role in _ROLE_FALLBACK:
        # First pass: shift covers now AND skill match.
        candidates = [
            (s, e)
            for s, e in by_role[role]
            if _shift_covers_now(s, now) and _engineer_has_skills(e, required)
        ]
        # Second pass: drop the skill requirement if first pass was empty.
        if not candidates and required:
            candidates = [(s, e) for s, e in by_role[role] if _shift_covers_now(s, now)]
        if not candidates:
            continue

        # Deterministic pick: lowest engineer id wins. Cheap proxy for
        # "longest-tenured." Replace with load-aware selection when we
        # add the oncall_load table (v2).
        _shift, chosen = min(candidates, key=lambda pair: pair[1].id or 0)
        logger.info(
            "oncall: %s -> engineer=%r role=%r (matched_skills=%s)",
            team,
            chosen.name,
            role,
            sorted(set(chosen.skills) & set(required)),
        )
        return OnCallEngineer(
            id=chosen.id or -1,
            name=chosen.name,
            email=chosen.email,
            slack_handle=chosen.slack_handle,
            slack_user_id=chosen.slack_user_id,
            team=team,
            role=role,
            skills=chosen.skills,
            timezone=chosen.timezone,
        )

    # Team-specific ladder exhausted; try the global wildcard rung so
    # services on un-onboarded teams still page someone on Sev-1.
    fallback = _find_global_escalation(team, now=now, required_skills=required)
    if fallback is not None:
        return fallback

    logger.info(
        "oncall: no engineer found for team=%r at %s (no team coverage, "
        "no wildcard escalation either — Sev-1 on this team would page nobody)",
        team,
        now.isoformat(),
    )
    return None


def _score_expertise(row: EngineerExpertiseRow) -> float:
    """Combine proficiency, track record, feedback, and operator override.

    The formula is deliberately legible — every term has a real-world
    interpretation so an operator reading a log line can reason about why
    one engineer outranked another:

      * **proficiency** — base bracket (novice/intermediate/expert/principal)
      * **incidents_resolved** (capped) — domain track record on this category
      * **feedback_score** (1.0–5.0) — quality of prior resolutions
      * **manual_priority** — operator override (rarely > 0); a strong nudge
    """
    base = _PROFICIENCY_WEIGHTS.get((row.proficiency_level or "").strip().lower(), 30.0)
    incidents = min(max(row.incidents_resolved, 0), _INCIDENTS_CAP) * 2.0
    feedback = max(0.0, min(row.feedback_score, 5.0)) * 20.0
    manual = max(row.manual_priority, 0) * 50.0
    return base + incidents + feedback + manual


def _match_categories_for_team(
    session: Session, team: str, keywords: list[str]
) -> list[tuple[FailureCategoryRow, int]]:
    """Pick categories within ``team`` whose keyword set overlaps inputs.

    Returns ``(category, overlap_count)`` pairs ordered by descending
    overlap. Overlap count is what differentiates a category that matched
    only the team marker (e.g. "payment" is in every Payments-* category)
    from one that matched a domain-specific term (e.g. "database" only
    lives in payment-database). Without weighting, all three Payments
    categories tie on the bare word "payment" and the strongest-scoring
    *engineer* wins — even if they're an expert in the wrong sub-area.

    Keyword matching is case-insensitive. Cross-team categories are never
    matched: routing has already picked a team (RA-002 owns that), so we
    only refine *within* that team's sub-domains.
    """
    if not keywords:
        return []
    want = {k.strip().lower() for k in keywords if k.strip()}
    if not want:
        return []
    cats = session.exec(select(FailureCategoryRow).where(FailureCategoryRow.team == team)).all()
    matches: list[tuple[FailureCategoryRow, int]] = []
    for c in cats:
        overlap = len(set(c.keywords) & want)
        if overlap:
            matches.append((c, overlap))
    matches.sort(key=lambda pair: pair[1], reverse=True)
    return matches


def find_best_for_team_and_category(
    team: str,
    category_keywords: list[str],
    *,
    now: datetime,
    required_skills: list[str] | None = None,
) -> OnCallEngineer | None:
    """Expertise-aware on-call lookup.

    Layered on top of :func:`find_oncall_for_team`:

    1. Match ``category_keywords`` against this team's failure categories.
       Categories are sub-domains (e.g. ``payment-gateway`` vs
       ``payment-database``) — same team, different specialty.
    2. Among engineers on shift right now for this team, find those with
       expertise in any matched category and pick the highest scorer
       (see :func:`_score_expertise`). Role fallback (primary → secondary
       → manager_escalation) is preserved.
    3. If no engineer with matching expertise is on shift in any bucket,
       degrade to a plain :func:`find_oncall_for_team` lookup so an alert
       is **never dropped** because the specialist happens to be off-shift.

    Why this design: matching live alert keywords to a structured
    specialty list (rather than letting the LLM guess) keeps routing
    explainable and testable. Operators can read one log line and know
    *why* this engineer was chosen over the other on-shift teammate.
    """
    keywords = [k for k in (category_keywords or []) if k and k.strip()]
    if not keywords:
        return find_oncall_for_team(team, now=now, required_skills=required_skills)

    required = list(required_skills or [])

    with Session(get_engine()) as session:
        matched_pairs = _match_categories_for_team(session, team, keywords)
        if not matched_pairs:
            logger.info(
                "oncall(expertise): team=%r no category matched keywords=%s; "
                "falling back to plain on-call lookup",
                team,
                keywords,
            )
            return find_oncall_for_team(team, now=now, required_skills=required)

        matched_cats = [c for c, _ in matched_pairs]
        matched_cat_ids = [c.id or 0 for c in matched_cats]
        overlap_by_cat_id: dict[int, int] = {(c.id or 0): n for c, n in matched_pairs}

        # One round-trip: shift × engineer × expertise for matched categories.
        rows = session.exec(
            select(ShiftRow, EngineerRow, EngineerExpertiseRow)
            .join(EngineerRow, ShiftRow.engineer_id == EngineerRow.id)
            .join(
                EngineerExpertiseRow,
                EngineerExpertiseRow.engineer_id == EngineerRow.id,
            )
            .where(ShiftRow.team == team)
            .where(EngineerRow.active.is_(True))  # type: ignore[attr-defined]
            .where(EngineerExpertiseRow.category_id.in_(matched_cat_ids))  # type: ignore[attr-defined]
        ).all()

    # Group (engineer_id) → (engineer, best_weighted_score, best_category_id)
    # within each role bucket. The score is weighted by the matching
    # category's overlap count — so an engineer who's expert in the
    # *specific* sub-area beats one who's merely an expert in some other
    # sub-area that matched only the generic team marker.
    by_role: dict[str, dict[int, tuple[EngineerRow, float, int]]] = {r: {} for r in _ROLE_FALLBACK}
    for shift, eng, exp in rows:
        if not _shift_covers_now(shift, now):
            continue
        if not _engineer_has_skills(eng, required):
            continue
        bucket = by_role.get(shift.role)
        if bucket is None:
            continue
        overlap = overlap_by_cat_id.get(exp.category_id, 1)
        s = _score_expertise(exp) * overlap
        prev = bucket.get(eng.id or 0)
        if prev is None or prev[1] < s:
            bucket[eng.id or 0] = (eng, s, exp.category_id)

    by_id = {c.id or 0: c for c in matched_cats}
    matched_names = [f"{c.name}(x{overlap_by_cat_id.get(c.id or 0, 0)})" for c in matched_cats]
    # The alert's *primary* sub-domain is the highest-overlap matched
    # category. We surface that on the on-call result (regardless of the
    # picked engineer's expertise area) because the Slack reader cares
    # about what the system thinks failed — not what the picked engineer
    # happens to be best at. ``matched_cats`` is sorted by overlap desc
    # in _match_categories_for_team.
    alert_top_cat = matched_cats[0]
    for role in _ROLE_FALLBACK:
        bucket = by_role[role]
        if not bucket:
            continue
        # Highest weighted score wins; tie-break on lowest engineer id
        # (proxy for longest-tenured, matches find_oncall_for_team rule).
        _chosen_id, (chosen, score, best_cat_id) = max(
            bucket.items(), key=lambda kv: (kv[1][1], -kv[0])
        )
        best_cat = by_id.get(best_cat_id)
        logger.info(
            "oncall(expertise): team=%r matched=%s -> engineer=%r "
            "weighted_score=%.1f role=%r alert_subdomain=%r "
            "engineer_strength=%r",
            team,
            matched_names,
            chosen.name,
            score,
            role,
            alert_top_cat.name,
            best_cat.name if best_cat else None,
        )
        return OnCallEngineer(
            id=chosen.id or -1,
            name=chosen.name,
            email=chosen.email,
            slack_handle=chosen.slack_handle,
            slack_user_id=chosen.slack_user_id,
            team=team,
            role=role,
            skills=chosen.skills,
            timezone=chosen.timezone,
            matched_category=alert_top_cat.name,
            matched_category_display=alert_top_cat.display_name,
        )

    # No on-shift expert in any role bucket — fall back to plain lookup
    # so the alert is still routed somewhere.
    logger.info(
        "oncall(expertise): no on-shift expert for team=%r matched=%s; "
        "falling back to plain on-call lookup",
        team,
        matched_names,
    )
    return find_oncall_for_team(team, now=now, required_skills=required)


__all__ = [
    "OnCallEngineer",
    "find_best_for_team_and_category",
    "find_oncall_for_team",
]
