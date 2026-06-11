"""Tests for the on-call DB + tool provider (issue ON-CALL-1/2/3).

Three layers tested:

1. ``aiops/state/oncall_repository`` — SQL-backed lookups:
   ``find_oncall_for_team`` (shift + skill + role-fallback) and
   ``find_best_for_team_and_category`` (overlap-weighted expertise).
2. ``aiops/tools/oncall.db_oncall_lookup`` — the tool provider wrapping
   the repository for the agent-facing ``oncall.schedule.lookup``
   capability; accepts ``category_keywords``.
3. RA-005's ``_resolve_oncall`` + ``_mentions_from`` consume
   ``slack_handle`` and ``matched_category_display`` from the lookup,
   falling back to the engineer's email when no handle is recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session

from aiops import state as state_pkg
from aiops.state import get_engine
from aiops.state.models import (
    EngineerExpertiseRow,
    EngineerRow,
    FailureCategoryRow,
    ShiftRow,
)
from aiops.state.oncall_repository import (
    OnCallEngineer,
    find_best_for_team_and_category,
    find_oncall_for_team,
)

# ─── Test fixtures ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _in_memory_db(monkeypatch):
    """Use an in-memory SQLite DB per test. State is fully ephemeral."""
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


def _seed_engineer(
    session: Session,
    *,
    name: str,
    team: str,
    skills: str = "",
    slack_handle: str | None = None,
    slack_user_id: str | None = None,
    email: str | None = None,
) -> EngineerRow:
    row = EngineerRow(
        name=name,
        email=email or f"{name.lower()}@example.com",
        slack_handle=slack_handle,
        slack_user_id=slack_user_id,
        primary_team=team,
        skills_csv=skills,
        timezone="UTC",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _seed_shift(
    session: Session,
    *,
    engineer_id: int,
    team: str,
    day_of_week: int,
    start_hour_utc: int,
    end_hour_utc: int,
    role: str = "primary",
) -> None:
    session.add(
        ShiftRow(
            engineer_id=engineer_id,
            team=team,
            day_of_week=day_of_week,
            start_hour_utc=start_hour_utc,
            end_hour_utc=end_hour_utc,
            role=role,
        )
    )
    session.commit()


# ─── Repository tests ─────────────────────────────────────────────────────


def test_returns_none_for_unknown_team():
    """Unknown team → None. No exception."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    assert find_oncall_for_team("Nonexistent Team", now=now) is None


def test_picks_primary_engineer_on_shift():
    """Primary engineer whose shift covers ``now`` is returned."""
    with Session(get_engine()) as s:
        e = _seed_engineer(s, name="Chinmay", team="Payments", skills="payments")
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )

    # Monday 06:00 UTC
    r = find_oncall_for_team("Payments", now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert r is not None and r.name == "Chinmay"
    assert r.role == "primary"


def test_skips_engineer_outside_shift_hours():
    """An engineer whose shift doesn't cover ``now`` is not returned."""
    with Session(get_engine()) as s:
        e = _seed_engineer(s, name="Chinmay", team="Payments")
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )

    # Monday 18:00 UTC — outside the 03..12 shift
    r = find_oncall_for_team("Payments", now=datetime(2026, 5, 18, 18, 0, tzinfo=UTC))
    assert r is None


def test_falls_back_to_secondary_when_no_primary():
    """No primary on shift → secondary takes over."""
    with Session(get_engine()) as s:
        primary = _seed_engineer(s, name="Chinmay", team="Payments")
        secondary = _seed_engineer(s, name="Riya", team="Payments")
        # Primary's shift is Tuesday only
        _seed_shift(
            s,
            engineer_id=primary.id or 0,
            team="Payments",
            day_of_week=1,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        # Secondary's shift is Monday
        _seed_shift(
            s,
            engineer_id=secondary.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="secondary",
        )

    r = find_oncall_for_team("Payments", now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert r is not None and r.name == "Riya"
    assert r.role == "secondary"


def test_manager_escalation_is_always_on():
    """``manager_escalation`` role is treated as always-on regardless of
    the stored shift hours."""
    with Session(get_engine()) as s:
        mgr = _seed_engineer(s, name="Vikram", team="Web Experience")
        # Stored as a single weekday entry but the role flag makes it always-on.
        _seed_shift(
            s,
            engineer_id=mgr.id or 0,
            team="Web Experience",
            day_of_week=0,
            start_hour_utc=0,
            end_hour_utc=1,
            role="manager_escalation",
        )

    # Saturday 22:00 UTC — well outside the stored hours
    r = find_oncall_for_team("Web Experience", now=datetime(2026, 5, 23, 22, 0, tzinfo=UTC))
    assert r is not None and r.name == "Vikram"
    assert r.role == "manager_escalation"


def test_skill_match_picks_skilled_engineer():
    """Required skill filters to the engineer who has it."""
    with Session(get_engine()) as s:
        with_kafka = _seed_engineer(s, name="Chinmay", team="Payments", skills="payments,kafka")
        without_kafka = _seed_engineer(
            s, name="Riya", team="Payments", skills="payments,kubernetes"
        )
        # Both on shift Monday morning
        _seed_shift(
            s,
            engineer_id=with_kafka.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        _seed_shift(
            s,
            engineer_id=without_kafka.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )

    r = find_oncall_for_team(
        "Payments",
        now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
        required_skills=["kafka"],
    )
    assert r is not None and r.name == "Chinmay"


def test_skill_filter_falls_back_when_no_one_matches():
    """If no one on shift has the required skill, wake someone unskilled
    rather than nobody (CLAUDE.md anti-fatigue heuristic)."""
    with Session(get_engine()) as s:
        eng = _seed_engineer(s, name="Riya", team="Payments", skills="kubernetes")
        _seed_shift(
            s,
            engineer_id=eng.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
        )

    r = find_oncall_for_team(
        "Payments",
        now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
        required_skills=["kafka"],  # nobody has kafka
    )
    assert r is not None and r.name == "Riya"


def test_inactive_engineers_are_excluded():
    """``active=False`` engineers must never be returned."""
    with Session(get_engine()) as s:
        eng = _seed_engineer(s, name="Chinmay", team="Payments")
        eng.active = False
        s.add(eng)
        s.commit()
        _seed_shift(
            s,
            engineer_id=eng.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
        )

    r = find_oncall_for_team("Payments", now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert r is None


def test_returned_record_carries_slack_handle():
    """The repo result includes slack_handle + slack_user_id so callers
    can build real-ping Slack messages."""
    with Session(get_engine()) as s:
        e = _seed_engineer(
            s, name="Chinmay", team="Payments", slack_handle="@chinmay", slack_user_id="U01ABC"
        )
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
        )

    r = find_oncall_for_team("Payments", now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert isinstance(r, OnCallEngineer)
    assert r.slack_handle == "@chinmay"
    assert r.slack_user_id == "U01ABC"


# ─── Tool provider tests ──────────────────────────────────────────────────


def test_db_oncall_lookup_tool_returns_rich_data():
    """The tool provider wraps the repo and returns the full data envelope."""
    with Session(get_engine()) as s:
        e = _seed_engineer(
            s,
            name="Chinmay",
            team="Payments",
            slack_handle="@chinmay",
            slack_user_id="U01ABC",
            skills="payments",
        )
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments",
            day_of_week=datetime.now(UTC).weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
        )

    from aiops.tools.oncall import db_oncall_lookup

    result = db_oncall_lookup(team="Payments")
    assert result.ok is True
    assert result.data["engineer_email"] == "chinmay@example.com"
    assert result.data["engineer_name"] == "Chinmay"
    assert result.data["slack_handle"] == "@chinmay"
    assert result.data["slack_user_id"] == "U01ABC"
    assert "payments" in result.data["skills"]
    assert result.metadata["provider"] == "db"
    assert result.metadata["matched"] is True


def test_db_oncall_lookup_tool_returns_null_engineer_on_miss():
    """No engineer for the team → returns a result with engineer_email=None,
    NOT an error. Lets agents degrade gracefully without try/except."""
    from aiops.tools.oncall import db_oncall_lookup

    result = db_oncall_lookup(team="Nonexistent")
    assert result.ok is True
    assert result.data["engineer_email"] is None
    assert result.data["slack_handle"] is None
    assert result.metadata["matched"] is False


# ─── RA-005 integration ───────────────────────────────────────────────────


def test_ra005_uses_slack_handle_from_oncall_lookup(monkeypatch):
    """When the on-call lookup returns a slack_handle, RA-005 uses it as
    the mention — that's what the Slack adapter rewrites to <@U…>."""
    # Seed a payments engineer with a Slack handle.
    with Session(get_engine()) as s:
        e = _seed_engineer(
            s, name="Chinmay", team="Payments Team", slack_handle="@chinmay", slack_user_id="U01ABC"
        )
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments Team",
            day_of_week=datetime.now(UTC).weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
        )

    # Activate the DB provider for this test (server lifespan does this in prod).
    import aiops.tools.oncall  # noqa: F401 — triggers @tool registration
    from aiops.tools import get_registry

    get_registry().select_provider("oncall.schedule.lookup", "db.oncall.schedule.lookup")

    # Build a minimal triage verdict whose assigned_engineer is the
    # synthetic email RA-001 produces; RA-005 should still pick the
    # slack handle for the mention.
    from agents.alert_triage import AuditMetadata, TriageVerdict
    from agents.notification_router import decide

    verdict = TriageVerdict(
        affected_service="payment",
        severity="Sev-2",  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary="payment error rate elevated",
        assigned_team="Payments Team",
        assigned_engineer="chinmay@example.com",  # what RA-001 set
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )

    d = decide(verdict, now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert d.mentions == ["@chinmay"]


def test_ra005_falls_back_to_email_when_no_slack_handle(monkeypatch):
    """If the on-call DB has no slack_handle for this team, RA-005 still
    produces a readable mention from the engineer's email."""
    with Session(get_engine()) as s:
        e = _seed_engineer(
            s, name="Anonymous", team="Payments Team", slack_handle=None, slack_user_id=None
        )
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments Team",
            day_of_week=datetime.now(UTC).weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
        )

    import aiops.tools.oncall  # noqa: F401
    from aiops.tools import get_registry

    get_registry().select_provider("oncall.schedule.lookup", "db.oncall.schedule.lookup")

    from agents.alert_triage import AuditMetadata, TriageVerdict
    from agents.notification_router import decide

    verdict = TriageVerdict(
        affected_service="payment",
        severity="Sev-2",  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary="payment error rate elevated",
        assigned_team="Payments Team",
        assigned_engineer="anonymous@example.com",
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )

    d = decide(verdict, now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert d.mentions == ["@anonymous@example.com"]


# ─── Expertise routing (sub-domain) ──────────────────────────────────────


def _seed_category(
    session: Session,
    *,
    name: str,
    team: str,
    keywords_csv: str,
    display_name: str | None = None,
) -> FailureCategoryRow:
    row = FailureCategoryRow(
        name=name,
        display_name=display_name or name,
        description="",
        team=team,
        keywords_csv=keywords_csv,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def _seed_expertise(
    session: Session,
    *,
    engineer_id: int,
    category_id: int,
    proficiency: str = "expert",
    incidents: int = 0,
    feedback: float = 4.0,
    manual: int = 0,
) -> None:
    session.add(
        EngineerExpertiseRow(
            engineer_id=engineer_id,
            category_id=category_id,
            proficiency_level=proficiency,
            incidents_resolved=incidents,
            feedback_score=feedback,
            manual_priority=manual,
        )
    )
    session.commit()


def test_expertise_picks_specialist_over_generalist():
    """Two engineers on shift; one is the gateway expert → expert wins."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        gateway_expert = _seed_engineer(s, name="Chinmay", team="Payments")
        generalist = _seed_engineer(s, name="Khushi", team="Payments")
        # Both on shift Monday morning.
        for eng in (gateway_expert, generalist):
            _seed_shift(
                s,
                engineer_id=eng.id or 0,
                team="Payments",
                day_of_week=0,
                start_hour_utc=3,
                end_hour_utc=12,
                role="primary",
            )
        gateway = _seed_category(
            s, name="payment-gateway", team="Payments", keywords_csv="payment,gateway,5xx"
        )
        # Only Chinmay has gateway expertise.
        _seed_expertise(
            s,
            engineer_id=gateway_expert.id or 0,
            category_id=gateway.id or 0,
            proficiency="expert",
            incidents=15,
            feedback=4.5,
        )

    r = find_best_for_team_and_category("Payments", ["payment", "gateway"], now=now)
    assert r is not None and r.name == "Chinmay"


def test_expertise_higher_score_wins_when_both_have_expertise():
    """Both have gateway expertise, but one scores higher → higher wins."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        strong = _seed_engineer(s, name="Strong", team="Payments")
        weak = _seed_engineer(s, name="Weak", team="Payments")
        for eng in (strong, weak):
            _seed_shift(
                s,
                engineer_id=eng.id or 0,
                team="Payments",
                day_of_week=0,
                start_hour_utc=3,
                end_hour_utc=12,
                role="primary",
            )
        cat = _seed_category(
            s, name="payment-gateway", team="Payments", keywords_csv="payment,gateway"
        )
        # Strong: expert, 20 incidents, 4.8 feedback
        _seed_expertise(
            s,
            engineer_id=strong.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=20,
            feedback=4.8,
        )
        # Weak: intermediate, 2 incidents, 3.5 feedback
        _seed_expertise(
            s,
            engineer_id=weak.id or 0,
            category_id=cat.id or 0,
            proficiency="intermediate",
            incidents=2,
            feedback=3.5,
        )

    r = find_best_for_team_and_category("Payments", ["gateway"], now=now)
    assert r is not None and r.name == "Strong"


def test_expertise_manual_priority_overrides_score():
    """Operator-set manual_priority dominates the scoring formula — a
    knob for ad-hoc overrides ("Khushi is the SME for THIS incident")."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        organic = _seed_engineer(s, name="Organic", team="Payments")
        forced = _seed_engineer(s, name="Forced", team="Payments")
        for eng in (organic, forced):
            _seed_shift(
                s,
                engineer_id=eng.id or 0,
                team="Payments",
                day_of_week=0,
                start_hour_utc=3,
                end_hour_utc=12,
                role="primary",
            )
        cat = _seed_category(s, name="payment-gateway", team="Payments", keywords_csv="gateway")
        # Organic: expert, 25 incidents, 5.0 feedback — naturally strong.
        _seed_expertise(
            s,
            engineer_id=organic.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=25,
            feedback=5.0,
        )
        # Forced: novice, 0 incidents, 3.0 feedback — would lose, but
        # manual_priority pins them to top.
        _seed_expertise(
            s,
            engineer_id=forced.id or 0,
            category_id=cat.id or 0,
            proficiency="novice",
            incidents=0,
            feedback=3.0,
            manual=5,
        )

    r = find_best_for_team_and_category("Payments", ["gateway"], now=now)
    assert r is not None and r.name == "Forced"


def test_expertise_falls_back_to_plain_when_no_keyword_match():
    """Keywords that don't match any seeded category → behaves like
    find_oncall_for_team. Critical: alerts must never be dropped."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        e = _seed_engineer(s, name="OnCall", team="Payments")
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        # Seed a category whose keywords DON'T match the input.
        _seed_category(s, name="payment-database", team="Payments", keywords_csv="database,sql")

    r = find_best_for_team_and_category("Payments", ["totally-unrelated-noise"], now=now)
    assert r is not None and r.name == "OnCall"


def test_expertise_falls_back_when_no_expert_on_shift():
    """Specialist exists in the DB but is off-shift → plain on-call lookup
    still returns whoever IS on shift. (Don't drop alerts.)"""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        specialist = _seed_engineer(s, name="Specialist", team="Payments")
        bystander = _seed_engineer(s, name="Bystander", team="Payments")
        # Specialist on Tuesday only.
        _seed_shift(
            s,
            engineer_id=specialist.id or 0,
            team="Payments",
            day_of_week=1,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        # Bystander on Monday.
        _seed_shift(
            s,
            engineer_id=bystander.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        cat = _seed_category(s, name="payment-gateway", team="Payments", keywords_csv="gateway")
        # Only specialist has the expertise.
        _seed_expertise(
            s,
            engineer_id=specialist.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=10,
            feedback=4.5,
        )

    r = find_best_for_team_and_category("Payments", ["gateway"], now=now)
    assert r is not None and r.name == "Bystander"


def test_expertise_does_not_cross_team_boundaries():
    """A category belongs to one team; same-name keyword on a different
    team must not match. (Routing already picked the team.)"""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        wrong_team_expert = _seed_engineer(s, name="WrongTeam", team="Platform")
        right_team_oncall = _seed_engineer(s, name="RightTeam", team="Payments")
        _seed_shift(
            s,
            engineer_id=wrong_team_expert.id or 0,
            team="Platform",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        _seed_shift(
            s,
            engineer_id=right_team_oncall.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        # Category lives in the Platform team.
        cat = _seed_category(
            s, name="kubernetes-platform", team="Platform", keywords_csv="kubernetes,pod"
        )
        _seed_expertise(
            s,
            engineer_id=wrong_team_expert.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=20,
            feedback=4.7,
        )

    # Routing has already decided this is a Payments incident. The
    # Platform expert must not steal it.
    r = find_best_for_team_and_category("Payments", ["kubernetes", "pod"], now=now)
    assert r is not None and r.name == "RightTeam"


def test_expertise_empty_keywords_uses_plain_lookup():
    """Empty / blank keyword list → straight delegation to the plain lookup."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        e = _seed_engineer(s, name="Plain", team="Payments")
        _seed_shift(
            s,
            engineer_id=e.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )

    assert find_best_for_team_and_category("Payments", [], now=now).name == "Plain"
    assert find_best_for_team_and_category("Payments", ["", "   "], now=now).name == "Plain"


def test_expertise_skill_filter_still_applies():
    """``required_skills`` filtering still narrows candidates BEFORE the
    expertise picker. The expert without the skill loses to the on-shift
    person with it. (Skill = hard requirement; expertise = soft preference.)"""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        unskilled_expert = _seed_engineer(s, name="Unskilled", team="Payments", skills="generic")
        skilled_novice = _seed_engineer(s, name="Skilled", team="Payments", skills="kafka")
        for eng in (unskilled_expert, skilled_novice):
            _seed_shift(
                s,
                engineer_id=eng.id or 0,
                team="Payments",
                day_of_week=0,
                start_hour_utc=3,
                end_hour_utc=12,
                role="primary",
            )
        cat = _seed_category(s, name="payment-gateway", team="Payments", keywords_csv="gateway")
        # Expert without the skill — should be filtered out.
        _seed_expertise(
            s,
            engineer_id=unskilled_expert.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=30,
            feedback=5.0,
        )

    r = find_best_for_team_and_category("Payments", ["gateway"], now=now, required_skills=["kafka"])
    assert r is not None and r.name == "Skilled"


# ─── Tool-level expertise passthrough ────────────────────────────────────


def test_db_oncall_lookup_tool_accepts_category_keywords():
    """The tool surface accepts category_keywords and echoes them in metadata."""
    with Session(get_engine()) as s:
        gateway_expert = _seed_engineer(
            s, name="Gateway", team="Payments", slack_handle="@gateway", slack_user_id="U0GATE"
        )
        plain = _seed_engineer(
            s, name="Plain", team="Payments", slack_handle="@plain", slack_user_id="U0PLAIN"
        )
        for eng in (gateway_expert, plain):
            _seed_shift(
                s,
                engineer_id=eng.id or 0,
                team="Payments",
                day_of_week=datetime.now(UTC).weekday(),
                start_hour_utc=0,
                end_hour_utc=24,
                role="primary",
            )
        cat = _seed_category(s, name="payment-gateway", team="Payments", keywords_csv="gateway")
        _seed_expertise(
            s,
            engineer_id=gateway_expert.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=15,
            feedback=4.5,
        )

    from aiops.tools.oncall import db_oncall_lookup

    r = db_oncall_lookup(team="Payments", category_keywords=["gateway"])
    assert r.ok is True
    assert r.data["engineer_name"] == "Gateway"
    assert r.data["slack_handle"] == "@gateway"
    assert r.metadata["category_keywords"] == ["gateway"]

    # Without keywords → falls back to plain shift lookup (lowest id).
    r2 = db_oncall_lookup(team="Payments")
    assert r2.ok is True
    assert r2.data["engineer_name"] in {"Gateway", "Plain"}
    assert r2.metadata["category_keywords"] == []


# ─── RA-005 keyword derivation ───────────────────────────────────────────


def test_ra005_category_keywords_extracted_from_verdict():
    """``_category_keywords_for`` tokenizes service, summary, runbook."""
    from agents.alert_triage import AuditMetadata, TriageVerdict
    from agents.notification_router.agent import _category_keywords_for

    v = TriageVerdict(
        affected_service="payment",
        severity="Sev-1",  # type: ignore[arg-type]
        confidence_score=0.95,
        alert_summary="Payment Gateway returning 5xx burst",
        assigned_team="Payments Team",
        assigned_engineer="x@y.com",
        recommended_runbook="rb-payment-gateway-restart",
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )

    kws = _category_keywords_for(v)
    # Must include domain terms from each source; case-folded; deduped.
    assert "payment" in kws
    assert "gateway" in kws
    assert "5xx" in kws
    assert "restart" in kws
    # Token order stable, deduped.
    assert kws == list(dict.fromkeys(kws))


def test_ra005_picks_specialist_via_expertise_routing(monkeypatch):
    """Two engineers on shift; the gateway specialist gets the mention."""
    with Session(get_engine()) as s:
        specialist = _seed_engineer(
            s,
            name="Specialist",
            team="Payments Team",
            slack_handle="@specialist",
            slack_user_id="U0SPEC",
        )
        generalist = _seed_engineer(
            s,
            name="Generalist",
            team="Payments Team",
            slack_handle="@generalist",
            slack_user_id="U0GEN",
        )
        for eng in (specialist, generalist):
            _seed_shift(
                s,
                engineer_id=eng.id or 0,
                team="Payments Team",
                day_of_week=datetime.now(UTC).weekday(),
                start_hour_utc=0,
                end_hour_utc=24,
                role="primary",
            )
        cat = _seed_category(
            s, name="payment-gateway", team="Payments Team", keywords_csv="gateway,5xx"
        )
        _seed_expertise(
            s,
            engineer_id=specialist.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=20,
            feedback=4.7,
        )

    import aiops.tools.oncall  # noqa: F401 — triggers @tool registration
    from aiops.tools import get_registry

    get_registry().select_provider("oncall.schedule.lookup", "db.oncall.schedule.lookup")

    from agents.alert_triage import AuditMetadata, TriageVerdict
    from agents.notification_router import decide

    verdict = TriageVerdict(
        affected_service="payment",
        severity="Sev-2",  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary="Payment gateway 5xx spike",
        assigned_team="Payments Team",
        assigned_engineer="generalist@example.com",  # RA-001's pick — overridden
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )

    d = decide(verdict, now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert d.mentions == ["@specialist"]
    # Matched sub-domain is surfaced on the decision so Slack can render it
    # as a dedicated "Sub-domain" field and the body gets a clean header.
    assert d.category_display == "payment-gateway"
    assert "Sub-domain: payment-gateway" in d.body
    assert "Application: payment" in d.body
    assert "What failed:" in d.body
    assert "paged for payment-gateway" in d.body  # On-call line names sub-domain


def test_ra005_body_has_no_subdomain_line_when_no_match():
    """When no category matched, the body should *not* invent a Sub-domain
    line — only render fields we actually have."""
    from agents.alert_triage import AuditMetadata, TriageVerdict
    from agents.notification_router import decide

    v = TriageVerdict(
        affected_service="payment",
        severity="Sev-2",  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary="payment elevated error rate",
        assigned_team="Payments Team",
        assigned_engineer="someone@example.com",
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )
    d = decide(v, now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert d.category_display is None
    assert "Sub-domain:" not in d.body
    # Core fields still present.
    assert "Application: payment" in d.body
    assert "What failed: payment elevated error rate" in d.body


def test_slack_bot_dm_includes_subdomain_field(tmp_path):
    """The bot adapter renders ``category_display`` as a dedicated field."""
    import json as _json
    from unittest.mock import patch

    import httpx

    from aiops.tools.chatops import ChatMessage, Severity
    from aiops.tools.chatops.adapters.slack_bot import SlackBotAdapter

    user_map = tmp_path / "slack_users.json"
    user_map.write_text(_json.dumps({"chinmay-kotkar": "U0CHINMAY"}), encoding="utf-8")
    adapter = SlackBotAdapter("xoxb-FAKE-TEST-TOKEN", user_map_path=user_map)

    msg = ChatMessage(
        channel="incidents",
        severity=Severity.P1,
        title="payment gateway 5xx burst",
        body="What failed: payment gateway 5xx burst\nApplication: payment\nSub-domain: Payment Gateway",
        incident_id="INC-77",
        service="payment",
        category_display="Payment Gateway",
        mentions=["@chinmay-kotkar"],
        actions=["page_oncall"],
    )

    fake_resp = httpx.Response(
        200,
        json={"ok": True, "ts": "1"},
        request=httpx.Request("POST", "https://slack.com/api/chat.postMessage"),
    )
    with patch(
        "aiops.tools.chatops.adapters.slack_bot.httpx.post",
        return_value=fake_resp,
    ) as p:
        adapter.send(msg)

    payload = p.call_args.kwargs["json"]
    blocks = payload["attachments"][0]["blocks"]
    fields_section = next((b for b in blocks if b["type"] == "section" and "fields" in b), None)
    assert fields_section is not None
    field_texts = " ".join(f["text"] for f in fields_section["fields"])
    assert "Application:" in field_texts and "payment" in field_texts
    assert "Sub-domain:" in field_texts and "Payment Gateway" in field_texts


# ─── Global wildcard fallback (never drop a Sev-1) ───────────────────────


def test_wildcard_escalation_engages_for_un_onboarded_team():
    """A team with zero engineers but a wildcard escalation in the DB
    still pages the wildcard engineer — the never-drop rung."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        commander = _seed_engineer(s, name="Commander", team="Platform")
        # Wildcard manager_escalation: team='*', always-on.
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=now.weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )

    # No engineer / shift exists for "Ads Team" — wildcard MUST take over.
    r = find_oncall_for_team("Ads Team", now=now)
    assert r is not None and r.name == "Commander"
    assert r.role == "manager_escalation"
    # Team context preserved so the audit trail shows what was queried.
    assert r.team == "Ads Team"


def test_team_specific_primary_beats_wildcard():
    """When a team-specific primary IS on shift, the wildcard must not
    fire. Wildcard is the floor, not a priority override."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        primary = _seed_engineer(s, name="TeamPrimary", team="Payments")
        commander = _seed_engineer(s, name="Commander", team="Platform")
        _seed_shift(
            s,
            engineer_id=primary.id or 0,
            team="Payments",
            day_of_week=now.weekday(),
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=now.weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )

    r = find_oncall_for_team("Payments", now=now)
    assert r is not None and r.name == "TeamPrimary"
    assert r.role == "primary"


def test_wildcard_falls_back_when_team_primary_off_shift():
    """Team has a primary but the primary's shift doesn't cover now,
    AND the team has no team-specific escalation. Wildcard fires."""
    now = datetime(2026, 5, 18, 18, 0, tzinfo=UTC)  # Monday evening
    with Session(get_engine()) as s:
        primary = _seed_engineer(s, name="OffShiftPrimary", team="Payments")
        commander = _seed_engineer(s, name="Commander", team="Platform")
        # Primary's morning shift doesn't cover 18:00.
        _seed_shift(
            s,
            engineer_id=primary.id or 0,
            team="Payments",
            day_of_week=0,
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=0,
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )

    r = find_oncall_for_team("Payments", now=now)
    assert r is not None and r.name == "Commander"
    assert r.role == "manager_escalation"


def test_expertise_routing_inherits_wildcard_fallback():
    """find_best_for_team_and_category for an un-onboarded team with no
    categories must still page someone via the wildcard rung. (Routing
    via expertise must not silently drop alerts on unknown teams.)"""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        commander = _seed_engineer(s, name="Commander", team="Platform")
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=now.weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )

    r = find_best_for_team_and_category("Ads Team", ["ad", "latency", "high"], now=now)
    assert r is not None and r.name == "Commander"
    assert r.role == "manager_escalation"


def test_wildcard_only_engages_for_manager_escalation_role():
    """A wildcard shift with role!='manager_escalation' must NOT fire
    the fallback — only true safety-net shifts are wildcard-eligible.
    Guards against a primary on team='*' accidentally hijacking every
    team's lookup."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        # An engineer on a wildcard PRIMARY shift — should be ignored.
        accidental = _seed_engineer(s, name="Accidental", team="Platform")
        _seed_shift(
            s,
            engineer_id=accidental.id or 0,
            team="*",
            day_of_week=now.weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="primary",
        )

    r = find_oncall_for_team("Ads Team", now=now)
    assert r is None


# ─── Wildcard discriminator + sub-domain threading (PR #167 self-review) ─


def test_wildcard_returned_engineer_carries_via_wildcard_flag():
    """OnCallEngineer.via_wildcard must be True iff the global wildcard
    rung fired. Without this discriminator, downstream consumers can't
    tell a team-specific manager_escalation row apart from the platform
    safety net (both produce role='manager_escalation')."""
    now = datetime(2026, 5, 18, 6, 0, tzinfo=UTC)
    with Session(get_engine()) as s:
        team_primary = _seed_engineer(s, name="TeamPrimary", team="Payments")
        commander = _seed_engineer(s, name="Commander", team="Platform")
        _seed_shift(
            s,
            engineer_id=team_primary.id or 0,
            team="Payments",
            day_of_week=now.weekday(),
            start_hour_utc=3,
            end_hour_utc=12,
            role="primary",
        )
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=now.weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )

    # Team-specific path: via_wildcard MUST be False.
    r_team = find_oncall_for_team("Payments", now=now)
    assert r_team is not None and r_team.name == "TeamPrimary"
    assert r_team.via_wildcard is False, "team-specific primary must NOT be marked via_wildcard"

    # Wildcard path: via_wildcard MUST be True.
    r_wildcard = find_oncall_for_team("Ads Team", now=now)
    assert r_wildcard is not None and r_wildcard.name == "Commander"
    assert r_wildcard.via_wildcard is True
    assert r_wildcard.role == "manager_escalation"


def test_expertise_wildcard_fallback_preserves_matched_category():
    """When find_best_for_team_and_category falls through to the wildcard
    rung (specialist off-shift), the alert's primary sub-domain must
    survive on the returned OnCallEngineer.matched_category so RA-005
    can render the Sub-domain Slack field. This regression was caught
    by the self-review of PR #167."""
    sat = datetime(
        2026, 5, 23, 6, 0, tzinfo=UTC
    )  # Saturday — primary's Mon-Fri shift doesn't cover
    with Session(get_engine()) as s:
        specialist = _seed_engineer(s, name="Specialist", team="Payments")
        commander = _seed_engineer(s, name="Commander", team="Platform")
        # Specialist only on Mon-Fri 03-12; nobody else on Payments.
        for dow in range(0, 5):
            _seed_shift(
                s,
                engineer_id=specialist.id or 0,
                team="Payments",
                day_of_week=dow,
                start_hour_utc=3,
                end_hour_utc=12,
                role="primary",
            )
        # Global wildcard always-on.
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=sat.weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )
        cat = _seed_category(
            s,
            name="payment-gateway",
            team="Payments",
            keywords_csv="payment,gateway,5xx",
        )
        _seed_expertise(
            s,
            engineer_id=specialist.id or 0,
            category_id=cat.id or 0,
            proficiency="expert",
            incidents=15,
            feedback=4.5,
        )

    # Saturday: specialist off-shift, no team primary on shift. Expertise
    # match was payment-gateway (overlap weight 3). Fallback should hit
    # the wildcard but PRESERVE the matched category.
    r = find_best_for_team_and_category("Payments", ["payment", "gateway", "5xx"], now=sat)
    assert r is not None
    assert r.name == "Commander", "wildcard rung must serve the page"
    assert r.via_wildcard is True
    assert r.matched_category == "payment-gateway", (
        "matched_category must survive the wildcard fallback so the Slack "
        "Sub-domain field still renders"
    )
    assert r.matched_category_display == "payment-gateway"


def test_db_oncall_lookup_tool_carries_via_wildcard():
    """The tool layer's data dict must include via_wildcard so RA-005
    can read it from the lookup result without a second round-trip."""
    with Session(get_engine()) as s:
        commander = _seed_engineer(
            s,
            name="Commander",
            team="Platform",
            slack_handle="@commander",
            slack_user_id="U0CMD",
        )
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=datetime.now(UTC).weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )

    from aiops.tools.oncall import db_oncall_lookup

    r = db_oncall_lookup(team="Ads Team")
    assert r.ok is True
    assert r.data["via_wildcard"] is True
    assert r.data["engineer_name"] == "Commander"
    assert r.data["role"] == "manager_escalation"


def test_ra005_body_marks_platform_escalation_when_via_wildcard(monkeypatch):
    """RA-005's rendered body must surface the wildcard origin so the
    paged engineer knows they're the platform safety net, not the team
    owner. Without this signal, the page is ambiguous: role='manager_
    escalation' alone can be a team-specific escalation OR the global
    fallback."""
    with Session(get_engine()) as s:
        commander = _seed_engineer(
            s,
            name="Commander",
            team="Platform",
            slack_handle="@commander",
            slack_user_id="U0CMD",
        )
        _seed_shift(
            s,
            engineer_id=commander.id or 0,
            team="*",
            day_of_week=datetime.now(UTC).weekday(),
            start_hour_utc=0,
            end_hour_utc=24,
            role="manager_escalation",
        )

    import aiops.tools.oncall  # noqa: F401  — registers the DB provider
    from aiops.tools import get_registry

    get_registry().select_provider("oncall.schedule.lookup", "db.oncall.schedule.lookup")

    from agents.alert_triage import AuditMetadata, TriageVerdict
    from agents.notification_router import decide

    verdict = TriageVerdict(
        affected_service="ad",
        severity="Sev-1",  # type: ignore[arg-type]
        confidence_score=0.9,
        alert_summary="ad service producing errors",
        assigned_team="Ads Team",
        assigned_engineer="someone@example.com",
        status="Active",
        audit_metadata=AuditMetadata(
            created_at=datetime(2026, 5, 18, 6, 0, tzinfo=UTC),
            source_alerts=["a-1"],
        ),
    )

    d = decide(verdict, now=datetime(2026, 5, 18, 6, 0, tzinfo=UTC))
    assert "platform escalation" in d.body, (
        "body must surface platform escalation for wildcard-served pages"
    )
    assert "Ads Team" in d.body, "body must name the team that's missing coverage"
    assert d.mentions == ["@commander"]
