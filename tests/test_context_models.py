"""Tests for the Context Engineering Layer's models and Phase-0 primitives.

The load-bearing test here is ``test_every_collection_field_is_a_tuple``. The whole
design rests on "agents consume the context, they never modify it", and frozen
Pydantic models only get you half of that — ``frozen=True`` blocks attribute
rebinding but a ``list`` field is still mutable in place, so one agent could append
to another's evidence with nothing to stop it. Rather than assert that on the eight
fields that exist today, it walks ``model_fields`` and fails on any *future* field
declared as a list, which is the version that still works after someone adds a
section next quarter.
"""

from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from aiops.context import cache, config, correlation
from aiops.context.models import (
    Observation,
    SectionSpec,
    SectionStatus,
    Source,
    digest,
    make_observation_id,
)
from aiops.context.pack import (
    ContextSection,
    IncidentContext,
    IncidentIdentity,
    RankedObservation,
    SecurityMetadata,
    SourceProvenance,
    TokenBudget,
)

WINDOW_START = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(minutes=15)


# --- fixtures / builders -------------------------------------------------


def _observation(**overrides) -> Observation:
    base = {
        "observation_id": make_observation_id("corr1", "logs", "error_log", "db timeout"),
        "correlation_id": "corr1",
        "source": "logs",
        "timestamp": WINDOW_START,
        "service": "payment-service",
        "severity": "error",
        "category": "error_log",
        "signature": "db timeout",
        "evidence": "connection to mysql timed out after 5s",
        "confidence": 0.8,
    }
    return Observation(**{**base, **overrides})


def _section(status: SectionStatus = SectionStatus.NOT_REQUESTED, **overrides) -> ContextSection:
    base = {
        "status": status,
        "provenance": SourceProvenance(provider="mock", status=status),
    }
    return ContextSection(**{**base, **overrides})


def _pack(**overrides) -> IncidentContext:
    base = {
        "incident": IncidentIdentity(
            service="payment-service",
            severity="Sev-2",
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            correlation_id="corr1",
        ),
        "built_at": WINDOW_END,
        "metrics": _section(),
        "logs": _section(),
        "traces": _section(),
        "k8s_events": _section(),
        "topology": _section(),
        "dependencies": _section(),
        "deployments": _section(),
        "incident_history": _section(),
        "oncall": _section(),
        "cmdb": _section(),
        "runbooks": _section(),
        "security": SecurityMetadata(redaction_applied=False),
    }
    return IncidentContext(**{**base, **overrides})


# --- immutability --------------------------------------------------------

_PACK_MODELS = (
    Observation,
    SectionSpec,
    ContextSection,
    IncidentIdentity,
    RankedObservation,
    SecurityMetadata,
    TokenBudget,
    SourceProvenance,
    IncidentContext,
)


@pytest.mark.parametrize("model", _PACK_MODELS, ids=lambda m: m.__name__)
def test_models_are_frozen_and_reject_unknown_fields(model: type[BaseModel]):
    assert model.model_config.get("frozen") is True, f"{model.__name__} must be frozen"
    assert model.model_config.get("extra") == "forbid", f"{model.__name__} must forbid extras"


def _is_list_like(annotation: object) -> bool:
    """True when a type annotation admits a mutable sequence anywhere inside it.

    Recurses through unions and generic parameters so ``list[str] | None``,
    ``tuple[list[str], ...]`` and ``dict[str, list[str]]`` are all caught, not just
    a bare ``list[...]``. A ``dict`` origin is not itself a failure (see the calling
    test for why) but its parameters are still inspected.
    """
    origin = typing.get_origin(annotation)
    if origin in (list, set):
        return True
    if origin is not None:
        return any(_is_list_like(arg) for arg in typing.get_args(annotation))
    return annotation in (list, set)


@pytest.mark.parametrize("model", _PACK_MODELS, ids=lambda m: m.__name__)
def test_every_collection_field_is_a_tuple(model: type[BaseModel]):
    """No model in the context carries a ``list`` or ``set`` field.

    ``frozen=True`` alone leaves a list field mutable in place, so this is what
    actually makes the context immutable in the way the design claims. Written
    against ``model_fields`` rather than a hard-coded field list so a section added
    later cannot reintroduce the hole without failing here.

    ``dict`` is the one accepted exception — ``Observation.metadata``,
    ``SecurityMetadata.redaction_counts`` and ``ContextSection.raw`` hold small,
    read-only, provider-echoed payloads, the same shallow-immutability compromise
    ``SupportingTelemetry`` and ``ChangeRecord`` already make in this repo.
    """
    offenders = [
        name for name, field in model.model_fields.items() if _is_list_like(field.annotation)
    ]
    assert not offenders, (
        f"{model.__name__} declares mutable collection field(s) {offenders}. "
        "Use tuple[...] — a frozen model with a list field is still mutable in place."
    )


def test_the_tuple_guard_actually_catches_a_list_field():
    """Meta-test: prove the guard above would fail if someone added a list field.

    A ratchet that silently ratchets nothing is worse than no ratchet, because it
    reads as coverage. This pins the detector itself.
    """
    assert _is_list_like(list[str])
    assert _is_list_like(list[str] | None)
    assert _is_list_like(tuple[list[str], ...])
    assert _is_list_like(dict[str, list[str]])
    assert not _is_list_like(tuple[str, ...])
    assert not _is_list_like(dict[str, object] | None)
    assert not _is_list_like(str)


def test_frozen_pack_rejects_attribute_assignment():
    pack = _pack()
    with pytest.raises(ValidationError):
        pack.schema_version = 2  # type: ignore[misc]


def test_observation_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        _observation(confidence=1.5)


# --- identity / determinism ---------------------------------------------


def test_digest_is_deterministic_and_short():
    assert digest("a", "b") == digest("a", "b")
    assert digest("a", "b") != digest("b", "a")
    assert len(digest("a")) == 16


def test_observation_id_ignores_non_identity_fields():
    """Two runs whose samples differ only in severity/timestamp are the same finding.

    The id answers "is this the same finding?", not "is this the same data?" — which
    is what lets a re-run be compared against its predecessor instead of merely
    replacing it.
    """
    first = _observation(severity="error", timestamp=WINDOW_START, confidence=0.9)
    second = _observation(severity="warn", timestamp=WINDOW_END, confidence=0.2)
    assert first.observation_id == second.observation_id


def test_correlation_id_is_stable_within_a_bucket(monkeypatch):
    """Windows seconds apart share an id, so two callers do not double every call."""
    monkeypatch.delenv("AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS", raising=False)
    a = correlation.derive_correlation_id("payment", WINDOW_START, WINDOW_END)
    b = correlation.derive_correlation_id(
        "payment", WINDOW_START + timedelta(seconds=3), WINDOW_END + timedelta(seconds=3)
    )
    assert a == b


def test_correlation_id_separates_distinct_incidents(monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS", raising=False)
    base = correlation.derive_correlation_id("payment", WINDOW_START, WINDOW_END)
    later = correlation.derive_correlation_id(
        "payment", WINDOW_START + timedelta(hours=1), WINDOW_END + timedelta(hours=1)
    )
    other_service = correlation.derive_correlation_id("checkout", WINDOW_START, WINDOW_END)
    assert base != later
    assert base != other_service


def test_correlation_id_normalises_service_name(monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS", raising=False)
    assert correlation.derive_correlation_id(
        "Payment", WINDOW_START, WINDOW_END
    ) == correlation.derive_correlation_id("  payment  ", WINDOW_START, WINDOW_END)


def test_bucket_seconds_falls_back_on_a_bad_value(monkeypatch):
    """A typo in an operator's .env must not take the incident path down."""
    monkeypatch.setenv("AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS", "not-a-number")
    assert correlation.bucket_seconds() == 60.0
    monkeypatch.setenv("AIOPS_CONTEXT_WINDOW_BUCKET_SECONDS", "0")
    assert correlation.bucket_seconds() == 60.0


# --- spec fingerprinting -------------------------------------------------


def test_fingerprint_ignores_query_id_but_not_params():
    """Two agents asking the identical question under different names share a key."""
    a = SectionSpec(source="metrics", query_id="rca.errors", params={"promql": "up"})
    b = SectionSpec(source="metrics", query_id="triage.errors", params={"promql": "up"})
    c = SectionSpec(source="metrics", query_id="rca.errors", params={"promql": "down"})
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_fingerprint_is_insensitive_to_param_ordering():
    a = SectionSpec(source="logs", query_id="q", params={"service": "pay", "limit": 200})
    b = SectionSpec(source="logs", query_id="q", params={"limit": 200, "service": "pay"})
    assert a.fingerprint() == b.fingerprint()


# --- status semantics ---------------------------------------------------


def test_status_separates_could_not_ask_from_asked_and_found_nothing():
    """The distinction RCA's prompt depends on.

    RCA renders "NONE — this signal was checked and was absent" and tells the model
    to treat it as evidence *against* a cause. That sentence is only true for
    ``EMPTY``; printing it for ``UNAVAILABLE`` would present a blind spot as a
    finding.
    """
    assert SectionStatus.EMPTY.usable
    assert SectionStatus.COLLECTED.usable
    assert not SectionStatus.UNAVAILABLE.usable
    assert not SectionStatus.FAILED.usable
    assert not SectionStatus.NOT_REQUESTED.usable

    assert SectionStatus.EMPTY.attempted
    assert SectionStatus.FAILED.attempted
    assert not SectionStatus.UNAVAILABLE.attempted
    assert not SectionStatus.NOT_REQUESTED.attempted


def test_status_serialises_as_its_string_value():
    """StrEnum so a status lands in a decision trace or ToolResult.metadata as-is."""
    assert f"{SectionStatus.COLLECTED}" == "collected"
    assert _section(SectionStatus.EMPTY).model_dump(mode="json")["status"] == "empty"


# --- pack accessors / serialisation ------------------------------------


def test_pack_round_trips_through_json():
    """Cacheable and carryable across a JSON boundary without a Python import."""
    pack = _pack(
        logs=_section(
            SectionStatus.COLLECTED,
            observations=(_observation(),),
            raw={"logs.recent": {"streams": []}},
        ),
        evidence_ranking=(
            RankedObservation(
                observation_id=_observation().observation_id,
                score=0.9,
                rank=1,
                rationale="cross-source agreement (logs+traces); 2m old",
            ),
        ),
    )
    restored = IncidentContext.model_validate(pack.model_dump(mode="json"))
    assert restored == pack


def test_every_source_has_a_section():
    """``IncidentContext.section()`` is total over the ``Source`` literal.

    Without this, adding a source to the literal and forgetting the field makes
    ``section("oncall")`` raise ``KeyError`` on the incident path — and only for
    whichever adapter happened to ask for it, so it would surface as one agent
    mysteriously losing its evidence rather than as an obvious modelling gap.
    """
    pack = _pack()
    declared = set(typing.get_args(Source))
    assert declared == set(pack.sections)
    for source in sorted(declared):
        assert pack.section(source) is not None  # type: ignore[arg-type]


def test_pack_section_lookup_and_aggregates():
    pack = _pack(
        logs=_section(SectionStatus.COLLECTED, observations=(_observation(),)),
        metrics=_section(SectionStatus.EMPTY),
    )
    assert pack.section("logs").status is SectionStatus.COLLECTED
    assert set(pack.usable_sources) == {"logs", "metrics"}
    assert len(pack.observations) == 1
    assert not pack.is_empty


def test_is_empty_is_true_when_nothing_was_collected():
    """The signal an adapter uses to fall through to its agent's legacy path."""
    assert _pack().is_empty
    assert _pack(logs=_section(SectionStatus.UNAVAILABLE)).is_empty


def test_sections_property_cannot_be_used_to_mutate_the_pack():
    pack = _pack()
    sections = pack.sections
    sections["logs"] = _section(SectionStatus.FAILED)
    assert pack.logs.status is SectionStatus.NOT_REQUESTED


def test_token_budget_is_absent_until_a_context_is_budgeted():
    """A context with token_budget=None has not been trimmed for anyone."""
    assert _pack().token_budget is None


# --- config -------------------------------------------------------------


def test_context_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("AIOPS_CONTEXT_LAYER", raising=False)
    assert config.context_mode() == "off"
    assert not config.enabled()
    assert not config.shadow_enabled()


@pytest.mark.parametrize("mode", ["off", "shadow", "on"])
def test_context_mode_accepts_each_valid_mode(monkeypatch, mode):
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", mode.upper() + " ")
    assert config.context_mode() == mode


def test_context_mode_degrades_to_off_on_a_typo(monkeypatch):
    """An unrecognised value must not take the incident path down."""
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "yes-please")
    assert config.context_mode() == "off"


def test_context_mode_is_read_per_call_not_at_import(monkeypatch):
    """The reason this repo needs no fourth _opt_in_enrichment_seams_off fixture.

    RA-007's three gates are import-time constants, so monkeypatch.delenv cannot
    reach them and conftest has to patch the module object instead. A per-call read
    responds to setenv/delenv normally.
    """
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "on")
    assert config.enabled()
    monkeypatch.setenv("AIOPS_CONTEXT_LAYER", "off")
    assert not config.enabled()


# --- cache --------------------------------------------------------------


def test_cache_keys_are_incident_scoped():
    """Without the correlation id in the key, a 60s TTL on an on-call lookup would
    serve one incident's engineer to the next — paging the wrong human."""
    spec = SectionSpec(source="oncall", query_id="primary", params={"team": "payments"})
    assert cache.section_key("incident-a", spec) != cache.section_key("incident-b", spec)
    assert "incident-a" in cache.section_key("incident-a", spec)


def test_cache_round_trips_a_collected_section():
    spec = SectionSpec(source="metrics", query_id="q", params={"promql": "up"})
    section = _section(SectionStatus.COLLECTED, observations=(_observation(),))

    assert cache.get("corr1", spec) == (False, None)
    cache.put("corr1", spec, section, SectionStatus.COLLECTED)
    hit, value = cache.get("corr1", spec)
    assert hit and value == section


@pytest.mark.parametrize(
    "status",
    [SectionStatus.FAILED, SectionStatus.UNAVAILABLE, SectionStatus.NOT_REQUESTED],
)
def test_failures_are_never_cached(status):
    """Caching a failure replays one dropped packet for the whole TTL window."""
    spec = SectionSpec(source="logs", query_id="q", params={"service": "pay"})
    cache.put("corr1", spec, _section(status), status)
    assert cache.get("corr1", spec) == (False, None)
    assert cache.ttl_for_status(status) == 0.0


def test_empty_results_get_a_shorter_ttl_than_collected_ones():
    """An empty answer is likelier to become non-empty than a positive one is to
    change, so it is rechecked sooner without being treated as a failure."""
    assert (
        0
        < cache.ttl_for_status(SectionStatus.EMPTY)
        < cache.ttl_for_status(SectionStatus.COLLECTED)
    )
