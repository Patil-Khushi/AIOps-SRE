"""Tests for stage 5 of the Context Engineering Layer — enrichment.

Zero mocks, by design: ``enrich`` is a pure function over sections, so every case here
is a literal payload in, a literal metadata dict out. If a test in this file ever needs
a patch or a fixture that reaches a provider, the stage has stopped being pure and that
is the bug, not the test.

Two groups of assertions carry most of the weight:

* **Absence.** Half of these tests assert that a key is *not* present. That is the point
  of the stage's contract — a consumer must be able to distinguish "not enriched" from
  "enriched with an empty value" — and a placeholder like ``"unknown"`` would pass every
  positive test in this file while destroying that distinction.
* **Change selection at the boundary.** ``recent_change`` is the highest-value key the
  stage writes and the easiest to get subtly wrong: off-by-one at the boundary, a
  coin-flip between two same-second changes, or an undated record treated as recent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from aiops.context.enricher import (
    ENRICHED_KEYS,
    ONCALL_ENGINEER,
    OWNING_TEAM,
    RECENT_CHANGE,
    RUNBOOK,
    enrich,
    ownership_of,
)
from aiops.context.models import Observation, SectionStatus, make_observation_id
from aiops.context.pack import ContextSection, SourceProvenance

ONSET = datetime(2026, 8, 10, 12, 30, 0, tzinfo=UTC)
SERVICE = "payment"


# --- builders ------------------------------------------------------------


def _observation(**overrides: Any) -> Observation:
    base: dict[str, Any] = {
        "observation_id": make_observation_id("corr1", "logs", "error_log", "db timeout"),
        "correlation_id": "corr1",
        "source": "logs",
        "timestamp": ONSET,
        "service": SERVICE,
        "severity": "error",
        "category": "error_log",
        "signature": "db timeout",
        "evidence": "connection to mysql timed out after 5s",
        "confidence": 0.8,
    }
    return Observation(**{**base, **overrides})


def _section(
    status: SectionStatus = SectionStatus.COLLECTED,
    *,
    provider: str = "mock",
    **overrides: Any,
) -> ContextSection:
    base: dict[str, Any] = {
        "status": status,
        "provenance": SourceProvenance(provider=provider, status=status),
    }
    return ContextSection(**{**base, **overrides})


def _cmdb(**payload: Any) -> ContextSection:
    data = {"service": SERVICE, "team": "Payments Team", "runbook": None, **payload}
    return _section(raw={"ownership": data})


def _oncall(**payload: Any) -> ContextSection:
    data = {
        "team": "Payments Team",
        "engineer_email": "asha@example.com",
        "engineer_name": "Asha Rao",
        **payload,
    }
    return _section(raw={"shift": data})


def _runbooks(**payload: Any) -> ContextSection:
    return _section(raw={"resolvers": {"service": SERVICE, **payload}})


def _record(
    change_id: str,
    timestamp: datetime | str | None,
    change_type: str = "deployment",
    **extra: Any,
) -> dict[str, Any]:
    """A change record in the shape ``ChangeRecord.model_dump(mode="json")`` produces."""
    moment = timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp
    return {
        "change_id": change_id,
        "change_type": change_type,
        "source": "github",
        "timestamp": moment,
        "service": SERVICE,
        "summary": f"{change_id} shipped",
        "rollback_status": "unknown",
        **extra,
    }


def _deployments(*records: dict[str, Any], status: SectionStatus = SectionStatus.COLLECTED):
    return _section(
        status,
        provider="github",
        raw={
            "changes": {
                "records": list(records),
                "sources_collected": ["github"],
                "sources_unavailable": [],
            }
        },
    )


def _sections(**overrides: Any) -> dict[str, ContextSection]:
    base: dict[str, ContextSection] = {
        "logs": _section(observations=(_observation(),)),
        "cmdb": _section(SectionStatus.NOT_REQUESTED),
        "oncall": _section(SectionStatus.NOT_REQUESTED),
        "runbooks": _section(SectionStatus.NOT_REQUESTED),
        "deployments": _section(SectionStatus.NOT_REQUESTED),
    }
    return {**base, **overrides}


def _first(sections: dict[str, ContextSection], name: str = "logs") -> Observation:
    return sections[name].observations[0]


# --- ownership -----------------------------------------------------------


def test_ownership_attached_when_cmdb_and_oncall_are_present() -> None:
    sections = _sections(
        cmdb=_cmdb(runbook="https://runbooks/payment"),
        oncall=_oncall(),
    )

    metadata = _first(enrich(sections, incident_service=SERVICE)).metadata

    assert metadata[OWNING_TEAM] == "Payments Team"
    assert metadata[ONCALL_ENGINEER] == "asha@example.com"
    assert metadata[RUNBOOK] == "https://runbooks/payment"


def test_ownership_keys_absent_when_the_sections_are_not_usable() -> None:
    """UNAVAILABLE and FAILED are not facts about the world — see SectionStatus."""
    sections = _sections(
        cmdb=_section(SectionStatus.FAILED, raw={"ownership": {"team": "Payments Team"}}),
        oncall=_section(SectionStatus.UNAVAILABLE, raw={"shift": {"engineer_email": "a@b.c"}}),
    )

    metadata = _first(enrich(sections, incident_service=SERVICE)).metadata

    assert OWNING_TEAM not in metadata
    assert ONCALL_ENGINEER not in metadata
    assert ownership_of(sections) == {}


def test_placeholder_values_are_never_attached() -> None:
    """A provider saying "unknown" in words has not told us who owns anything."""
    sections = _sections(
        cmdb=_cmdb(team="unknown", runbook="   "),
        oncall=_oncall(team="", engineer_email=None, engineer_name=None),
    )

    metadata = _first(enrich(sections, incident_service=SERVICE)).metadata

    assert ENRICHED_KEYS.isdisjoint(metadata)


def test_empty_section_payload_is_not_ownership() -> None:
    """An EMPTY CMDB lookup keeps its raw slot with a ``None`` payload — not a fact."""
    sections = _sections(cmdb=_section(SectionStatus.EMPTY, raw={"ownership": None}))

    assert ownership_of(sections) == {}


def test_ownership_of_reports_only_the_facts_it_has() -> None:
    """Nobody is on shift and no runbook was named, so those keys are simply absent —
    a caller must treat every key as optional rather than expecting three."""
    sections = _sections(oncall=_oncall(engineer_email=None, engineer_name=None))

    # The on-call payload still echoes the team it was asked about, which is a real fact:
    # something resolved that team name in order to ask about it.
    assert ownership_of(sections) == {OWNING_TEAM: "Payments Team"}


def test_a_display_name_stands_in_when_no_email_is_known() -> None:
    """Email is preferred because it is what actually routes, but a name still names
    someone a human can find."""
    sections = _sections(oncall=_oncall(engineer_email=None))

    assert ownership_of(sections)[ONCALL_ENGINEER] == "Asha Rao"


def test_runbooks_section_outranks_the_cmdb_runbook_column() -> None:
    sections = _sections(
        cmdb=_cmdb(runbook="https://cmdb/generic"),
        runbooks=_runbooks(runbooks=[{"title": "Payment DB failover", "url": "https://rb/db"}]),
    )

    assert ownership_of(sections)[RUNBOOK] == "https://rb/db"


def test_ownership_is_not_attached_to_another_services_observation() -> None:
    """Stamping the incident owner onto a neighbour's evidence asserts what the CMDB
    never claimed. The incident-level answer stays available via ``ownership_of``."""
    sections = _sections(
        logs=_section(observations=(_observation(service="cart"),)),
        cmdb=_cmdb(),
        oncall=_oncall(),
    )

    metadata = _first(enrich(sections, incident_service=SERVICE)).metadata

    assert OWNING_TEAM not in metadata
    assert ownership_of(sections)[OWNING_TEAM] == "Payments Team"


def test_telemetry_prefixed_service_still_matches_the_incident_service() -> None:
    """``service_name`` labels carry the deployment prefix; CMDB rows do not."""
    sections = _sections(
        logs=_section(observations=(_observation(service="ecommerce-payment"),)),
        cmdb=_cmdb(),
    )

    assert _first(enrich(sections, incident_service=SERVICE)).metadata[OWNING_TEAM]


def test_ownership_is_not_guessed_without_an_incident_service() -> None:
    sections = _sections(cmdb=_cmdb(), oncall=_oncall())

    metadata = _first(enrich(sections, incident_service="")).metadata

    assert OWNING_TEAM not in metadata


# --- recent change -------------------------------------------------------


def test_recent_change_is_the_closest_one_at_or_before_the_observation() -> None:
    sections = _sections(
        deployments=_deployments(
            _record("old", ONSET - timedelta(hours=2)),
            _record("closest", ONSET - timedelta(minutes=4)),
            _record("later", ONSET + timedelta(minutes=1)),
        )
    )

    change = _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE]

    assert change["change_id"] == "closest"
    assert change["age_seconds"] == 240
    assert change["timestamp"] == (ONSET - timedelta(minutes=4)).isoformat()
    # ``RollbackStatus.UNKNOWN`` means nobody looked, so it must not read as "still live".
    assert "rollback_status" not in change


def test_a_change_at_the_observation_timestamp_counts() -> None:
    """The boundary case this key exists for: a deploy stamped to the failing sample."""
    sections = _sections(deployments=_deployments(_record("simultaneous", ONSET)))

    change = _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE]

    assert change["change_id"] == "simultaneous"
    assert change["age_seconds"] == 0


def test_a_change_only_after_the_observation_is_not_attached() -> None:
    """Nothing shipped before this symptom, so no key at all — not an empty one."""
    sections = _sections(deployments=_deployments(_record("after", ONSET + timedelta(seconds=1))))

    assert RECENT_CHANGE not in _first(enrich(sections, incident_service=SERVICE)).metadata


def test_an_undated_change_is_never_selected() -> None:
    """An undated change cannot be shown to precede anything, so it is not ordered."""
    sections = _sections(
        deployments=_deployments(
            _record("undated", None),
            _record("unparseable", "last tuesday"),
        )
    )

    assert RECENT_CHANGE not in _first(enrich(sections, incident_service=SERVICE)).metadata


def test_a_deployment_outranks_a_commit_at_the_same_timestamp() -> None:
    """The SCM seam reports a commit and the deploy that carried it from one event;
    naming the commit would point a responder at code that may not have shipped."""
    same = ONSET - timedelta(minutes=1)
    sections = _sections(
        deployments=_deployments(
            _record("commit-1", same, change_type="commit"),
            _record("deploy-1", same, change_type="deployment"),
        )
    )

    change = _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE]

    assert change["change_id"] == "deploy-1"


def test_a_verified_username_is_preferred_over_a_git_config_author() -> None:
    sections = _sections(
        deployments=_deployments(_record("d1", ONSET, author="laptop user", author_username="arao"))
    )

    change = _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE]

    assert change["author"] == "arao"


def test_change_observations_do_not_carry_a_recent_change() -> None:
    """A deploy listing itself as its own antecedent reads as causality and says nothing."""
    deployments = _deployments(_record("d1", ONSET - timedelta(minutes=5)))
    deployments = deployments.model_copy(
        update={"observations": (_observation(source="deployments", category="deployment"),)}
    )
    sections = _sections(deployments=deployments)

    enriched = enrich(sections, incident_service=SERVICE)

    # ``.metadata`` matters: a pydantic model iterates as (name, value) pairs, so
    # ``key not in observation`` is vacuously true and would pass whatever this stage did.
    assert RECENT_CHANGE not in _first(enriched, "deployments").metadata


def test_naive_observation_timestamps_are_read_as_utc() -> None:
    """Mixing naive and aware datetimes raises TypeError; the stage must not fall over."""
    sections = _sections(
        logs=_section(observations=(_observation(timestamp=ONSET.replace(tzinfo=None)),)),
        deployments=_deployments(_record("d1", ONSET - timedelta(minutes=2))),
    )

    change = _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE]

    assert change["age_seconds"] == 120


def test_normalised_change_observations_are_used_when_raw_records_are_absent() -> None:
    """A context whose raw payloads were dropped must still enrich, rather than read as
    "nothing shipped"."""
    deployments = _section(
        provider="github",
        raw={"changes": {"sources_collected": ["github"]}},
        observations=(
            _observation(
                source="deployments",
                category="deployment",
                timestamp=ONSET - timedelta(minutes=3),
                evidence="deployed 9f1c2a to payment",
                metadata={"commit_sha": "9f1c2a"},
            ),
        ),
    )
    sections = _sections(deployments=deployments)

    change = _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE]

    assert change["commit_sha"] == "9f1c2a"
    assert change["age_seconds"] == 180


def test_a_malformed_deployment_payload_degrades_instead_of_raising() -> None:
    sections = _sections(
        deployments=_section(
            provider="github",
            raw={"changes": {"records": ["not-a-record", None, {"timestamp": 12345}]}},
        )
    )

    assert RECENT_CHANGE not in _first(enrich(sections, incident_service=SERVICE)).metadata


def test_an_unusable_deployments_section_yields_no_change() -> None:
    sections = _sections(
        deployments=_deployments(_record("d1", ONSET), status=SectionStatus.FAILED)
    )

    assert RECENT_CHANGE not in _first(enrich(sections, incident_service=SERVICE)).metadata


def test_the_real_change_record_shape_is_understood() -> None:
    """Pins the parser against the actual model rather than a hand-written echo of it, so
    a field rename in the change seam fails here instead of silently disabling the
    highest-value enrichment in the layer."""
    from aiops.tools.change_context.base import ChangeRecord, ChangeType

    record = ChangeRecord(
        change_id="gh-42",
        change_type=ChangeType.DEPLOYMENT,
        source="github",
        timestamp=ONSET - timedelta(minutes=6),
        service=SERVICE,
        summary="release 1.4.0",
        commit_sha="abc1234",
        url="https://github.test/deploy/42",
    )
    sections = _sections(deployments=_deployments(record.model_dump(mode="json")))

    change = _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE]

    assert change["change_id"] == "gh-42"
    assert change["change_type"] == "deployment"
    assert change["summary"] == "release 1.4.0"
    assert change["commit_sha"] == "abc1234"
    assert change["url"] == "https://github.test/deploy/42"
    assert change["age_seconds"] == 360


# --- contract ------------------------------------------------------------


def test_keys_set_upstream_are_never_overwritten() -> None:
    """The correlator's judgement outranks this stage's default, and a deliberate ``None``
    ("checked, unknown") is itself a claim — presence is the test, not truthiness."""
    sections = _sections(
        logs=_section(
            observations=(
                _observation(
                    metadata={
                        OWNING_TEAM: "Checkout Team",
                        RECENT_CHANGE: None,
                    }
                ),
            )
        ),
        cmdb=_cmdb(),
        oncall=_oncall(),
        deployments=_deployments(_record("d1", ONSET)),
    )

    metadata = _first(enrich(sections, incident_service=SERVICE)).metadata

    assert metadata[OWNING_TEAM] == "Checkout Team"
    assert metadata[RECENT_CHANGE] is None
    assert metadata[ONCALL_ENGINEER] == "asha@example.com"


def test_enrichment_writes_nothing_outside_its_own_vocabulary() -> None:
    sections = _sections(
        cmdb=_cmdb(runbook="https://rb/db"),
        oncall=_oncall(),
        deployments=_deployments(_record("d1", ONSET)),
    )

    metadata = _first(enrich(sections, incident_service=SERVICE)).metadata

    assert set(metadata) == set(ENRICHED_KEYS)


def test_inputs_are_never_mutated() -> None:
    original = _observation()
    sections = _sections(
        logs=_section(observations=(original,)),
        cmdb=_cmdb(),
        oncall=_oncall(),
        deployments=_deployments(_record("d1", ONSET - timedelta(minutes=1))),
    )
    before = {name: section.model_dump(mode="json") for name, section in sections.items()}

    enriched = enrich(sections, incident_service=SERVICE)

    assert original.metadata == {}
    assert enriched is not sections
    assert {name: s.model_dump(mode="json") for name, s in sections.items()} == before
    # And the enriched observation is a different object, so no consumer of the original
    # context can be surprised by a metadata dict that grew under it.
    assert _first(enriched) is not original


def test_sections_with_nothing_to_add_are_passed_through_by_identity() -> None:
    sections = _sections(
        logs=_section(SectionStatus.EMPTY),
        metrics=_section(SectionStatus.UNAVAILABLE, observations=()),
    )

    enriched = enrich(sections, incident_service=SERVICE)

    assert enriched["logs"] is sections["logs"]
    assert enriched["metrics"] is sections["metrics"]
    assert list(enriched) == list(sections)


def test_enrichment_is_deterministic() -> None:
    sections = _sections(
        cmdb=_cmdb(runbook="https://rb/db"),
        oncall=_oncall(),
        deployments=_deployments(
            _record("d2", ONSET - timedelta(minutes=1)),
            _record("d1", ONSET - timedelta(minutes=1), change_type="commit"),
        ),
    )

    first = enrich(sections, incident_service=SERVICE)
    second = enrich(sections, incident_service=SERVICE)

    assert {n: s.model_dump(mode="json") for n, s in first.items()} == {
        n: s.model_dump(mode="json") for n, s in second.items()
    }


def test_selection_does_not_depend_on_query_id_insertion_order() -> None:
    """A section can hold several queries; which collector merged first must not decide
    which change a responder is shown."""
    early = _record("early", ONSET - timedelta(minutes=9))
    late = _record("late", ONSET - timedelta(minutes=2))
    forwards = _section(
        provider="github",
        raw={"a_first": {"records": [early]}, "z_second": {"records": [late]}},
    )
    backwards = _section(
        provider="github",
        raw={"z_second": {"records": [late]}, "a_first": {"records": [early]}},
    )

    def selected(deployments: ContextSection) -> str:
        sections = _sections(deployments=deployments)
        return _first(enrich(sections, incident_service=SERVICE)).metadata[RECENT_CHANGE][
            "change_id"
        ]

    assert selected(forwards) == selected(backwards) == "late"
