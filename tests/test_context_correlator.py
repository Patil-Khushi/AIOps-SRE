"""Tests for stage 3 of the context pipeline — ``aiops.context.correlator``.

Three of these carry more weight than the rest:

* ``test_no_topology_reads_as_unknown_never_unrelated`` — the distinction the whole
  module exists to protect. If an empty dependency list ever renders as ``unrelated``,
  a missing CMDB record becomes a prompt sentence saying the service is irrelevant,
  and the ranker buries the evidence on the strength of a fabricated claim.
* ``test_alias_spellings_of_one_service_all_read_as_self`` — one service wears three
  names simultaneously in this repo (telemetry, truth files, CMDB). Naive ``==`` here
  mislabels a direct dependency as unrelated, which is the same failure with an extra
  step.
* ``test_non_usable_sections_are_left_untouched_and_uncounted`` — pins "absent is not
  empty" at this stage: a ``FAILED`` provider's leftovers must neither be annotated
  nor inflate anyone else's agreement count.

The stage is pure, so none of this needs a mock, a clock or a registry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from aiops.context.correlator import (
    correlate,
    cross_source_signatures,
    normalize_service_name,
    services_match,
)
from aiops.context.models import Observation, SectionStatus, make_observation_id
from aiops.context.pack import ContextSection, SourceProvenance

WINDOW_START = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


# --- builders ------------------------------------------------------------


def _observation(
    source: str = "logs",
    *,
    service: str = "payment-service",
    signature: str = "db timeout",
    **overrides: Any,
) -> Observation:
    base: dict[str, Any] = {
        "observation_id": make_observation_id("corr1", source, "error_log", signature),
        "correlation_id": "corr1",
        "source": source,
        "timestamp": WINDOW_START,
        "service": service,
        "severity": "error",
        "category": "error_log",
        "signature": signature,
        "evidence": "connection to mysql timed out after 5s",
        "confidence": 0.8,
    }
    base.update(overrides)
    return Observation(**base)


def _section(
    *observations: Observation,
    status: SectionStatus = SectionStatus.COLLECTED,
) -> ContextSection:
    return ContextSection(
        status=status,
        observations=observations,
        provenance=SourceProvenance(provider="mock", status=status),
    )


def _relation_of(observation: Observation, **kwargs: Any) -> Any:
    """Relation attached to a single observation placed in the ``logs`` section."""
    out = correlate({"logs": _section(observation)}, **kwargs)
    return out["logs"].observations[0].metadata["topology_relation"]


# --- cross-source agreement ---------------------------------------------


def test_same_signature_in_two_sources_is_recorded_on_both_observations():
    sections = {
        "logs": _section(_observation("logs")),
        "traces": _section(_observation("traces")),
        "metrics": _section(_observation("metrics", signature="cpu saturated")),
    }

    out = correlate(sections, incident_service="payment-service")

    for name in ("logs", "traces"):
        metadata = out[name].observations[0].metadata
        assert metadata["sources_agreeing"] == ("logs", "traces")
        assert metadata["occurrences"] == 2
    lone = out["metrics"].observations[0].metadata
    assert lone["sources_agreeing"] == ("metrics",)
    assert lone["occurrences"] == 1


def test_cross_source_signatures_reports_only_multi_source_signatures():
    sections = {
        "logs": _section(_observation("logs"), _observation("logs", signature="pool exhausted")),
        "traces": _section(_observation("traces")),
    }

    assert cross_source_signatures(sections) == {"db timeout": ("logs", "traces")}


def test_repeats_within_one_source_count_but_do_not_claim_agreement():
    """Twenty identical timeout lines is a volume fact, not corroboration."""
    sections = {"logs": _section(_observation("logs"), _observation("logs"))}

    out = correlate(sections, incident_service="payment-service")

    for observation in out["logs"].observations:
        assert observation.metadata["occurrences"] == 2
        assert observation.metadata["sources_agreeing"] == ("logs",)
    assert cross_source_signatures(sections) == {}


def test_blank_signatures_do_not_manufacture_agreement():
    """Two unsignatured observations must not read as the strongest signal available."""
    sections = {
        "logs": _section(_observation("logs", signature="")),
        "traces": _section(_observation("traces", signature="   ")),
    }

    out = correlate(sections, incident_service="payment-service")

    assert out["logs"].observations[0].metadata["sources_agreeing"] == ("logs",)
    assert out["logs"].observations[0].metadata["occurrences"] == 1
    assert out["traces"].observations[0].metadata["sources_agreeing"] == ("traces",)
    assert cross_source_signatures(sections) == {}


def test_signatures_differing_only_by_surrounding_whitespace_are_one_signature():
    sections = {
        "logs": _section(_observation("logs", signature="db timeout")),
        "traces": _section(_observation("traces", signature="  db timeout  ")),
    }

    assert cross_source_signatures(sections) == {"db timeout": ("logs", "traces")}


# --- topology relations --------------------------------------------------


def test_observation_about_the_failing_service_is_self():
    assert (
        _relation_of(
            _observation(service="payment-service"),
            incident_service="payment-service",
            dependencies=["redis"],
        )
        == "self"
    )


def test_service_on_the_dependency_list_is_a_dependency():
    assert (
        _relation_of(
            _observation(service="redis"),
            incident_service="payment-service",
            dependencies=["mock-payment-gateway", "redis"],
        )
        == "dependency"
    )


@pytest.mark.parametrize("edge_key", ["depends_on", "dependencies", "calls", "target_service"])
def test_an_observation_declaring_an_edge_into_the_incident_service_is_dependent(edge_key: str):
    """The only way an upstream caller can be recognised.

    ``dependencies`` is one-directional — it says what the incident service calls —
    so a reverse edge has to come from the observation itself.
    """
    observation = _observation(service="order-service", metadata={edge_key: ["payment-service"]})
    assert (
        _relation_of(
            observation,
            incident_service="payment-service",
            dependencies=["redis"],
        )
        == "dependent"
    )


def test_a_dependency_cycle_resolves_toward_the_topology_seams_answer():
    """A declares B and B declares A. The incident's own dependency list wins."""
    observation = _observation(service="redis", metadata={"depends_on": ["payment-service"]})
    assert (
        _relation_of(
            observation,
            incident_service="payment-service",
            dependencies=["redis"],
        )
        == "dependency"
    )


def test_a_named_non_neighbour_with_topology_present_is_unrelated():
    assert (
        _relation_of(
            _observation(service="recommendation"),
            incident_service="payment-service",
            dependencies=["redis", "mock-payment-gateway"],
        )
        == "unrelated"
    )


def test_no_topology_reads_as_unknown_never_unrelated():
    """An empty dependency list is the absence of an answer, not an answer.

    Conflating the two turns "the CMDB has no CI record for this service" into a
    prompt sentence asserting the service is irrelevant to the incident.
    """
    assert (
        _relation_of(
            _observation(service="recommendation"),
            incident_service="payment-service",
            dependencies=[],
        )
        == "unknown"
    )


def test_dependency_entries_that_name_nothing_do_not_count_as_topology():
    """``["", None]`` is an unusable list, not a list — so still ``unknown``.

    The ``None`` is not hypothetical: this list comes from a provider payload
    (``raw["dependencies"]``), and a stage that counted unnameable entries as topology
    would report ``unrelated`` — a claim — off the back of a malformed response.
    """
    assert (
        _relation_of(
            _observation(service="recommendation"),
            incident_service="payment-service",
            dependencies=["", "   ", None],
        )
        == "unknown"
    )


def test_an_observation_with_no_service_name_is_unknown_even_with_topology():
    assert (
        _relation_of(
            _observation(service=""),
            incident_service="payment-service",
            dependencies=["redis"],
        )
        == "unknown"
    )


def test_self_still_wins_when_no_topology_is_available():
    assert (
        _relation_of(
            _observation(service="payment"),
            incident_service="payment-service",
            dependencies=[],
        )
        == "self"
    )


# --- service-name alias tolerance ---------------------------------------

_ALIASES = ["payment", "payment-service", "ecommerce-payment-service", "Payment_Service"]


@pytest.mark.parametrize("observed", _ALIASES)
@pytest.mark.parametrize("incident", _ALIASES)
def test_alias_spellings_of_one_service_all_read_as_self(observed: str, incident: str):
    """One service, three live spellings.

    Telemetry labels carry ``ecommerce-payment-service`` (OTEL_SERVICE_NAME), truth
    files and alerts use ``payment-service``, parts of the CMDB graph use ``payment``.
    A naive ``==`` mislabels the failing service's own evidence as unrelated.
    """
    assert _relation_of(_observation(service=observed), incident_service=incident) == "self"


def test_alias_tolerance_extends_to_the_dependency_list():
    assert (
        _relation_of(
            _observation(service="ecommerce-payment-service"),
            incident_service="order-service",
            dependencies=["payment-service"],
        )
        == "dependency"
    )


def test_a_shared_token_that_is_not_the_tail_does_not_match():
    """``mock-payment-gateway`` is genuinely a different service from ``payment``."""
    assert not services_match("mock-payment-gateway", "payment")
    assert (
        _relation_of(
            _observation(service="mock-payment-gateway"),
            incident_service="payment-service",
            dependencies=["redis"],
        )
        == "unrelated"
    )


def test_punctuation_only_disagreements_match():
    assert services_match("product-catalog", "productcatalogservice")
    assert services_match("user_service", "user-service")
    assert services_match("  Redis ", "redis")


def test_an_unnamed_service_matches_nothing_including_another_unnamed_one():
    assert not services_match("", "payment")
    assert not services_match("", "")


def test_normalize_keeps_qualifying_prefixes():
    """Normalisation is for comparison, not aliasing — the prefix survives."""
    assert normalize_service_name("Payment_Service") == "payment"
    assert normalize_service_name("ecommerce-payment-service") == "ecommerce-payment"
    assert normalize_service_name("productcatalogservice") == "productcatalog"
    assert normalize_service_name("  ") == ""
    # Only the trailing "this thing is a service" suffix goes, and a service named
    # literally "service" stays nameable.
    assert normalize_service_name("service-mesh") == "service-mesh"
    assert normalize_service_name("service") == "service"


# --- status discipline ---------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [SectionStatus.FAILED, SectionStatus.UNAVAILABLE, SectionStatus.NOT_REQUESTED],
)
def test_non_usable_sections_are_left_untouched_and_uncounted(status: SectionStatus):
    """A ``FAILED`` provider's leftovers are neither trusted nor allowed to corroborate."""
    broken = _section(_observation("traces"), status=status)
    sections = {"logs": _section(_observation("logs")), "traces": broken}

    out = correlate(sections, incident_service="payment-service")

    assert out["traces"] is broken
    assert "topology_relation" not in out["traces"].observations[0].metadata
    assert out["logs"].observations[0].metadata["occurrences"] == 1
    assert out["logs"].observations[0].metadata["sources_agreeing"] == ("logs",)
    assert cross_source_signatures(sections) == {}


def test_an_empty_section_is_a_real_answer_and_survives_correlation():
    """``EMPTY`` is usable, so the section is carried through with its status intact."""
    sections = {"metrics": _section(status=SectionStatus.EMPTY)}
    out = correlate(sections, incident_service="payment-service")
    assert out["metrics"].status is SectionStatus.EMPTY


# --- purity / determinism ------------------------------------------------


def test_correlation_is_deterministic_across_runs():
    sections = {
        "logs": _section(_observation("logs"), _observation("logs", service="order-service")),
        "traces": _section(_observation("traces")),
        "metrics": _section(_observation("metrics", signature="cpu saturated")),
    }
    kwargs: dict[str, Any] = {
        "incident_service": "payment-service",
        "dependencies": ["redis", "mock-payment-gateway"],
    }

    first = correlate(sections, **kwargs)
    second = correlate(sections, **kwargs)

    assert [(name, section.model_dump(mode="json")) for name, section in first.items()] == [
        (name, section.model_dump(mode="json")) for name, section in second.items()
    ]


def test_correlating_twice_recomputes_rather_than_accumulates():
    """Idempotent, so a shadow-mode rebuild cannot leave a stale relation behind."""
    sections = {"logs": _section(_observation("logs"))}
    once = correlate(sections, incident_service="payment-service")
    twice = correlate(once, incident_service="payment-service")
    assert once["logs"].model_dump() == twice["logs"].model_dump()


def test_section_key_order_is_preserved():
    """The result feeds ``IncidentContext``'s field-per-source layout; reordering it
    would make two identical runs serialise differently."""
    sections = {
        "traces": _section(_observation("traces")),
        "logs": _section(_observation("logs")),
        "metrics": _section(status=SectionStatus.EMPTY),
    }
    assert list(correlate(sections, incident_service="payment-service")) == list(sections)


# --- inputs are never mutated -------------------------------------------


def test_inputs_are_never_mutated():
    original = _observation("logs", metadata={"pod": "payment-7f9"})
    section = _section(original)
    sections = {"logs": section}

    out = correlate(sections, incident_service="payment-service", dependencies=["redis"])

    assert original.metadata == {"pod": "payment-7f9"}
    assert section.observations[0] is original
    assert sections == {"logs": section}
    assert out is not sections
    # The annotated copy keeps what was already there and adds the three keys.
    annotated = out["logs"].observations[0].metadata
    assert annotated["pod"] == "payment-7f9"
    assert set(annotated) == {"pod", "topology_relation", "sources_agreeing", "occurrences"}


def test_mutating_the_returned_mapping_cannot_reach_the_input():
    sections = {"logs": _section(_observation("logs"))}
    out = correlate(sections, incident_service="payment-service")
    out["logs"] = _section(status=SectionStatus.FAILED)
    assert sections["logs"].status is SectionStatus.COLLECTED


# --- malformed payloads degrade rather than raise -----------------------


@pytest.mark.parametrize(
    "metadata",
    [
        {"depends_on": 7},
        {"depends_on": None},
        {"dependencies": [None, 3, {"service": "payment"}]},
        {"calls": {"nested": "payment-service"}},
    ],
)
def test_malformed_edge_metadata_degrades_to_fewer_relations_not_an_exception(
    metadata: dict[str, Any],
):
    """A provider payload surprise costs an edge, never the incident path."""
    observation = _observation(service="order-service", metadata=metadata)
    assert (
        _relation_of(
            observation,
            incident_service="payment-service",
            dependencies=["redis"],
        )
        == "unrelated"
    )


def test_an_empty_context_correlates_to_an_empty_context():
    assert correlate({}, incident_service="payment-service") == {}
    assert cross_source_signatures({}) == {}
