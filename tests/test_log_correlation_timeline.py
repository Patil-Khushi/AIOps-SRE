"""Tests for the incident evidence timeline (Phase 5).

The timeline's job is to make ordering legible: a latency spike at 10:03 means
something quite different if a rollout happened at 10:02, and that fact lives in
Kubernetes rather than in Loki. So the tests concentrate on the properties that
would silently destroy that reading:

- **Merging vs grouping.** Merging collapses repeats of *one* event; grouping tags
  *different* events as one story. Confusing the two either loses facts or loses
  order.
- **Provenance survival.** A merged entry must union its evidence ids, or the
  merge makes the timeline less traceable than the entries it replaced.
- **Honest absence.** No deployment entries can mean "nothing was deployed" or "we
  could not look". Those exonerate a deploy very differently, so the distinction
  is carried explicitly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import ClassVar

import pytest

from agents.log_correlation import CorrelationInput, correlate
from agents.log_correlation.timeline import (
    IncidentTimeline,
    TimelineEvent,
    build_timeline,
    group_related,
    merge_duplicates,
)
from agents.log_correlation.timeline_sources import from_evidence, from_topology

_T0 = datetime(2026, 7, 31, 10, 0, 0, tzinfo=UTC)


def _ev(
    offset_s: int, event: str, *, source="logs", service="checkout", severity="error", ids=None
):
    return TimelineEvent(
        timestamp=_T0 + timedelta(seconds=offset_s),
        event=event,
        service=service,
        severity=severity,
        source=source,
        related_evidence_ids=ids or [],
    )


def _window(minutes: int = 15) -> dict[str, str]:
    end = datetime.now(UTC)
    return {"start": (end - timedelta(minutes=minutes)).isoformat(), "end": end.isoformat()}


# ─── entry contract ──────────────────────────────────────────────────────────


def test_entry_carries_every_required_field():
    e = _ev(0, "boom", ids=["ev1"])
    assert isinstance(e.timestamp, datetime)
    assert e.event == "boom"
    assert e.service == "checkout"
    assert e.severity == "error"
    assert e.source == "logs"
    assert e.related_evidence_ids == ["ev1"]


def test_entries_are_immutable():
    e = _ev(0, "boom")
    with pytest.raises(Exception):
        e.event = "changed"


def test_timeline_is_immutable():
    t = build_timeline(correlation_id="c", service="checkout", events=[_ev(0, "a")])
    with pytest.raises(Exception):
        t.entries = []


# ─── chronological ordering ──────────────────────────────────────────────────


def test_entries_are_sorted_chronologically():
    events = [_ev(300, "third"), _ev(0, "first"), _ev(120, "second")]
    t = build_timeline(correlation_id="c", service="checkout", events=events)
    assert [e.event for e in t.entries] == ["first", "second", "third"]


def test_ordering_is_stable_when_timestamps_tie():
    """Identical timestamps must not reorder run to run, or the rendered trace
    differs between identical incidents."""
    events = [_ev(0, "b", source="traces"), _ev(0, "a", source="logs")]
    first = [
        e.event for e in build_timeline(correlation_id="c", service="s", events=events).entries
    ]
    for _ in range(3):
        again = build_timeline(correlation_id="c", service="s", events=events)
        assert [e.event for e in again.entries] == first


# ─── merging duplicates ──────────────────────────────────────────────────────


def test_identical_events_close_together_merge_with_a_count():
    """Fifty restart events in a minute is one fact with a count, not fifty facts."""
    events = [_ev(i, "BackOff: restarting", source="deployment") for i in range(50)]
    merged = merge_duplicates(events)

    assert len(merged) == 1
    assert merged[0].occurrences == 50


def test_merge_unions_evidence_ids():
    """Keeping only the first id would make the merged entry less traceable than
    the entries it replaced — the opposite of the field's purpose."""
    events = [_ev(0, "same", ids=["a"]), _ev(5, "same", ids=["b"]), _ev(10, "same", ids=["a"])]
    merged = merge_duplicates(events)

    assert len(merged) == 1
    assert merged[0].related_evidence_ids == ["a", "b"], "deduplicated union"


def test_merge_keeps_the_earliest_timestamp():
    """When an event started is what matters for ordering it against a deploy."""
    merged = merge_duplicates([_ev(30, "same"), _ev(0, "same"), _ev(50, "same")])
    assert merged[0].timestamp == _T0


def test_events_far_apart_do_not_merge():
    """Two bursts an hour apart are two incidents' worth of information."""
    merged = merge_duplicates([_ev(0, "same"), _ev(3600, "same")])
    assert len(merged) == 2


def test_different_sources_never_merge():
    """The same text from logs and from traces is corroboration — collapsing it
    would destroy the cross-source agreement that makes it strong."""
    merged = merge_duplicates([_ev(0, "same", source="logs"), _ev(0, "same", source="traces")])
    assert len(merged) == 2


def test_different_services_never_merge():
    merged = merge_duplicates(
        [_ev(0, "same", service="checkout"), _ev(0, "same", service="payment")]
    )
    assert len(merged) == 2


def test_different_severities_never_merge():
    merged = merge_duplicates([_ev(0, "same", severity="error"), _ev(0, "same", severity="info")])
    assert len(merged) == 2


# ─── grouping related events ─────────────────────────────────────────────────


def test_nearby_events_share_a_group_id_without_merging():
    """Grouping is not merging: a rollout and the errors after it are distinct
    facts whose order is the point."""
    events = [_ev(0, "deploy", source="deployment"), _ev(60, "errors", source="logs")]
    grouped = group_related(events)

    assert len(grouped) == 2, "must not collapse"
    assert grouped[0].group_id == grouped[1].group_id


def test_distant_events_get_different_groups():
    grouped = group_related([_ev(0, "a"), _ev(3600, "b")])
    assert grouped[0].group_id != grouped[1].group_id


def test_group_ids_are_deterministic():
    events = [_ev(0, "a"), _ev(30, "b")]
    first = [e.group_id for e in group_related(events)]
    assert [e.group_id for e in group_related(events)] == first


def test_grouping_empty_input_is_safe():
    assert group_related([]) == []


# ─── source preservation ─────────────────────────────────────────────────────


def test_source_is_preserved_per_entry():
    events = [
        _ev(0, "a", source="logs"),
        _ev(10, "b", source="metrics"),
        _ev(20, "c", source="traces"),
        _ev(30, "d", source="topology"),
        _ev(40, "e", source="deployment"),
        _ev(50, "f", source="configuration"),
    ]
    t = build_timeline(correlation_id="c", service="s", events=events)
    assert {e.source for e in t.entries} == {
        "logs",
        "metrics",
        "traces",
        "topology",
        "deployment",
        "configuration",
    }


def test_sources_present_lists_contributors():
    t = build_timeline(
        correlation_id="c",
        service="s",
        events=[_ev(0, "a", source="logs"), _ev(10, "b", source="deployment")],
    )
    assert t.sources_present == ["deployment", "logs"]


def test_change_events_are_selectable():
    """Most outages follow a change, so consumers must be able to pick those out."""
    events = [
        _ev(0, "deploy", source="deployment"),
        _ev(10, "flag flipped", source="configuration"),
        _ev(20, "errors", source="logs"),
    ]
    t = build_timeline(correlation_id="c", service="s", events=events)

    assert len(t.change_events) == 2
    assert {e.source for e in t.change_events} == {"deployment", "configuration"}


# ─── truncation ──────────────────────────────────────────────────────────────


def test_truncation_keeps_the_earliest_entries():
    """Dropping from the front would remove the trigger and keep the symptoms."""
    events = [_ev(i * 120, f"e{i}") for i in range(20)]
    t = build_timeline(correlation_id="c", service="s", events=events, max_entries=5)

    assert t.truncated is True
    assert len(t.entries) == 5
    assert t.entries[0].event == "e0"


def test_untruncated_timeline_says_so():
    t = build_timeline(correlation_id="c", service="s", events=[_ev(0, "a")], max_entries=10)
    assert t.truncated is False


# ─── source adapters ─────────────────────────────────────────────────────────


def test_from_evidence_links_back_to_evidence_ids():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    events = from_evidence(r.evidence)

    assert events
    for e in events:
        assert e.related_evidence_ids, "each telemetry entry must be traceable"


def test_from_evidence_attributes_to_the_implicated_service():
    """ "checkout: payment charge error" reads as a checkout fault. Naming the
    implicated service is what makes the timeline say what it means."""
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    services = {e.service for e in from_evidence(r.evidence)}
    assert "payment" in services


def test_from_topology_records_the_dependency_set():
    events = from_topology("checkout", ["payment", "cart"], _T0, provider="cmdb")
    assert len(events) == 1
    assert events[0].source == "topology"
    assert "payment" in events[0].event
    assert events[0].severity == "info", "context, not a symptom"


def test_from_topology_emits_nothing_without_dependencies():
    """An empty topology is not an event worth asserting."""
    assert from_topology("checkout", [], _T0) == []


@pytest.mark.parametrize(
    ("pod_name", "expected"),
    [
        # Every shape below was taken from the live cluster.
        ("frontend-5bff6448f5-cdcsw", "frontend"),
        # The phantom-attribution case: a prefix test gives this to "frontend".
        ("frontend-proxy-6d4948d448-7ttqp", "frontend-proxy"),
        # A first fix over-stripped this to "otel-collector" because "agent" is
        # five lowercase characters and looked like a pod hash.
        ("otel-collector-agent-6nbf6", "otel-collector-agent"),
        ("product-catalog-67897cb6f4-zpk24", "product-catalog"),
        ("grafana-image-renderer-558947fc57-fkvh6", "grafana-image-renderer"),
        ("valkey-cart-5c986c984d-b2sz5", "valkey-cart"),
        # StatefulSet ordinal.
        ("loki-0", "loki"),
        # Already a bare workload name.
        ("checkout", "checkout"),
    ],
)
def test_owner_name_recovers_the_workload(pod_name, expected):
    from agents.log_correlation.timeline_sources import owner_name

    assert owner_name(pod_name) == expected


def test_frontend_does_not_absorb_frontend_proxy_events(monkeypatch):
    """Regression guard for a bug found only by running against a real cluster.

    ``"frontend-proxy-...".startswith("frontend")`` is True, so a prefix filter
    puts frontend-proxy's restarts on frontend's timeline — another service's
    failure on this service's record.
    """
    from agents.log_correlation import timeline_sources as ts

    class _Obj:
        kind = "Pod"
        name = "frontend-proxy-6d4948d448-7ttqp"

    class _Item:
        involved_object = _Obj()
        reason = "BackOff"
        message = "restarting"
        last_timestamp = _T0 + timedelta(minutes=1)
        event_time = None
        first_timestamp = None

    class _Core:
        def list_namespaced_event(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_Item()]})()

        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": []})()

    monkeypatch.setattr(ts, "_K8S_ENABLED", True)
    monkeypatch.setattr(ts, "_k8s_clients", lambda: _Core())

    for_frontend, _ = ts.fetch_change_events("frontend", _T0, _T0 + timedelta(minutes=15))
    for_proxy, _ = ts.fetch_change_events("frontend-proxy", _T0, _T0 + timedelta(minutes=15))

    assert for_frontend == [], "frontend must not claim frontend-proxy's events"
    assert len(for_proxy) == 1, "the owner should get its own event"


@pytest.mark.parametrize("reason", ["SandboxChanged", "FailedMount", "Pulling", "FailedScheduling"])
def test_reasons_observed_live_are_not_dropped(reason, monkeypatch):
    """These four were being silently discarded — the namespace had 26
    SandboxChanged and 9 FailedMount events, and FailedMount is a real failure."""
    from agents.log_correlation import timeline_sources as ts

    assert reason in ts._DEPLOYMENT_REASONS


def test_configuration_events_come_from_managed_fields(monkeypatch):
    """Kubernetes emits no Event for a ConfigMap change — live inspection found
    only Pod-kind events — so an Event-based configuration source is dead code.
    ``managedFields`` is where the timestamps actually live.
    """
    from agents.log_correlation import timeline_sources as ts

    class _Field:
        manager = "helm"
        operation = "Apply"
        time = _T0 + timedelta(minutes=2)

    class _Meta:
        name = "flagd-config"
        managed_fields: ClassVar = [_Field()]

    class _CM:
        metadata = _Meta()

    class _Core:
        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_CM()]})()

    events = ts.fetch_configuration_events(_Core(), "checkout", _T0, _T0 + timedelta(minutes=15))

    assert len(events) == 1
    assert events[0].source == "configuration"
    assert "helm" in events[0].event
    assert events[0].severity == "warning", "a change is the usual trigger, not mere info"


def test_flagd_config_is_always_relevant(monkeypatch):
    """A feature-flag flip is how failures are injected in this demo, and it
    affects every service — not just one named after the ConfigMap."""
    from agents.log_correlation import timeline_sources as ts

    class _Field:
        manager = "kubectl"
        operation = "Update"
        time = _T0 + timedelta(minutes=1)

    class _Meta:
        name = "flagd-config"
        managed_fields: ClassVar = [_Field()]

    class _CM:
        metadata = _Meta()

    class _Core:
        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_CM()]})()

    for svc in ("checkout", "payment", "recommendation"):
        events = ts.fetch_configuration_events(_Core(), svc, _T0, _T0 + timedelta(minutes=15))
        assert len(events) == 1, f"flagd changes must reach {svc}"


def test_configmap_writes_outside_the_window_are_excluded():
    from agents.log_correlation import timeline_sources as ts

    class _Field:
        manager = "helm"
        operation = "Apply"
        time = _T0 - timedelta(days=3)

    class _Meta:
        name = "flagd-config"
        managed_fields: ClassVar = [_Field()]

    class _CM:
        metadata = _Meta()

    class _Core:
        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_CM()]})()

    assert ts.fetch_configuration_events(_Core(), "checkout", _T0, _T0 + timedelta(minutes=5)) == []


def test_configmap_listing_failure_is_contained():
    """A ConfigMap read failure must not lose the deployment events already
    gathered in the same call."""
    from agents.log_correlation import timeline_sources as ts

    class _Core:
        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            raise RuntimeError("forbidden")

    assert ts.fetch_configuration_events(_Core(), "checkout", _T0, _T0 + timedelta(minutes=5)) == []


def test_change_events_disabled_by_default_and_says_why(monkeypatch):
    """The critical distinction: an empty list plus a note, never a bare empty
    list that reads as "nothing was deployed"."""
    from agents.log_correlation import timeline_sources as ts

    monkeypatch.setattr(ts, "_K8S_ENABLED", False)
    events, note = ts.fetch_change_events("checkout", _T0, _T0 + timedelta(minutes=15))

    assert events == []
    assert note is not None and "disabled" in note


def test_change_events_unreachable_cluster_is_reported_not_silent(monkeypatch):
    from agents.log_correlation import timeline_sources as ts

    monkeypatch.setattr(ts, "_K8S_ENABLED", True)
    monkeypatch.setattr(ts, "_k8s_clients", lambda: None)
    events, note = ts.fetch_change_events("checkout", _T0, _T0 + timedelta(minutes=15))

    assert events == []
    assert note is not None and "unavailable" in note


def test_deployment_events_are_mapped_from_kube_events(monkeypatch):
    from agents.log_correlation import timeline_sources as ts

    class _Obj:
        kind = "Pod"
        name = "checkout-5d746fb948-vh9xh"

    class _Item:
        involved_object = _Obj()
        reason = "BackOff"
        message = "Back-off restarting failed container"
        last_timestamp = _T0 + timedelta(minutes=1)
        event_time = None
        first_timestamp = None

    class _Core:
        def list_namespaced_event(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_Item()]})()

        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": []})()

    monkeypatch.setattr(ts, "_K8S_ENABLED", True)
    monkeypatch.setattr(ts, "_k8s_clients", lambda: _Core())
    events, note = ts.fetch_change_events("checkout", _T0, _T0 + timedelta(minutes=15))

    assert note is None
    assert len(events) == 1
    assert events[0].source == "deployment"
    assert events[0].severity == "error", "BackOff is a failure, not information"


def test_configmap_events_are_classified_as_configuration(monkeypatch):
    from agents.log_correlation import timeline_sources as ts

    class _Obj:
        kind = "ConfigMap"
        name = "checkout-config"

    class _Item:
        involved_object = _Obj()
        reason = "Updated"
        message = "flag flipped"
        last_timestamp = _T0 + timedelta(minutes=2)
        event_time = None
        first_timestamp = None

    class _Core:
        def list_namespaced_event(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_Item()]})()

        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": []})()

    monkeypatch.setattr(ts, "_K8S_ENABLED", True)
    monkeypatch.setattr(ts, "_k8s_clients", lambda: _Core())
    events, _ = ts.fetch_change_events("checkout", _T0, _T0 + timedelta(minutes=15))

    assert len(events) == 1
    assert events[0].source == "configuration"


def test_events_outside_the_window_are_excluded(monkeypatch):
    """A rollout from last week is not part of this incident."""
    from agents.log_correlation import timeline_sources as ts

    class _Obj:
        kind = "Pod"
        name = "checkout-5d746fb948-vh9xh"

    class _Item:
        involved_object = _Obj()
        reason = "Killing"
        message = "m"
        last_timestamp = _T0 - timedelta(days=7)
        event_time = None
        first_timestamp = None

    class _Core:
        def list_namespaced_event(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_Item()]})()

        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": []})()

    monkeypatch.setattr(ts, "_K8S_ENABLED", True)
    monkeypatch.setattr(ts, "_k8s_clients", lambda: _Core())
    events, _ = ts.fetch_change_events("checkout", _T0, _T0 + timedelta(minutes=15))
    assert events == []


def test_unrelated_services_are_excluded(monkeypatch):
    from agents.log_correlation import timeline_sources as ts

    class _Obj:
        kind = "Pod"
        name = "recommendation-698856b74-2tqwm"

    class _Item:
        involved_object = _Obj()
        reason = "Killing"
        message = "m"
        last_timestamp = _T0 + timedelta(minutes=1)
        event_time = None
        first_timestamp = None

    class _Core:
        def list_namespaced_event(self, ns, timeout_seconds=None):
            return type("L", (), {"items": [_Item()]})()

        def list_namespaced_config_map(self, ns, timeout_seconds=None):
            return type("L", (), {"items": []})()

    monkeypatch.setattr(ts, "_K8S_ENABLED", True)
    monkeypatch.setattr(ts, "_k8s_clients", lambda: _Core())
    events, _ = ts.fetch_change_events("checkout", _T0, _T0 + timedelta(minutes=15))
    assert events == []


# ─── integration into the agent ──────────────────────────────────────────────


def test_correlate_populates_the_incident_timeline():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)

    assert isinstance(r.incident_timeline, IncidentTimeline)
    assert r.incident_timeline.entries
    assert "topology" in r.incident_timeline.sources_present


def test_incident_timeline_shares_the_evidence_correlation_id():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    assert r.incident_timeline.correlation_id == r.evidence[0].correlation_id


def test_existing_outputs_are_unchanged():
    """Phase 5 is additive: nothing that existed before may move."""
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)

    assert r.confidence == 0.9
    assert r.suspected_dependencies == ["payment"]
    assert len(r.timeline) == 3, "raw signal timeline unchanged"
    assert all(hasattr(s, "signature") for s in r.timeline), "still CorrelatedSignal objects"
    assert r.audit_metadata.created_by == "RA-007"


def test_raw_timeline_and_incident_timeline_are_different_things():
    """The two must not be conflated: one is raw signals, one is a merged account."""
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    assert type(r.timeline) is list
    assert isinstance(r.incident_timeline, IncidentTimeline)


def test_timeline_is_json_serializable():
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    dumped = r.model_dump(mode="json")

    assert dumped["incident_timeline"]["entries"]
    entry = dumped["incident_timeline"]["entries"][0]
    for field in ("timestamp", "event", "service", "severity", "source", "related_evidence_ids"):
        assert field in entry


def test_timeline_build_failure_does_not_lose_the_verdict(monkeypatch):
    from agents.log_correlation import agent as lc_agent

    def _boom(*_a, **_kw):
        raise RuntimeError("timeline exploded")

    monkeypatch.setattr(lc_agent, "_build_incident_timeline", _boom)
    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)

    assert r.incident_timeline is None
    assert r.suspected_dependencies == ["payment"], "verdict survives"
    assert any("timeline: build failed" in t for t in r.audit_metadata.decision_trace)


def test_render_is_capped_for_prompt_use():
    events = [_ev(i * 120, f"e{i}") for i in range(30)]
    t = build_timeline(correlation_id="c", service="s", events=events, max_entries=30)
    text = t.render(limit=5)

    assert text.count("\n") <= 5
    assert "omitted" in text


def test_absent_timeline_is_distinct_from_an_empty_one():
    """``None`` means not built; an empty entries list would claim nothing
    happened."""
    from agents.log_correlation.models import AuditMetadata, CorrelationResult

    r = CorrelationResult(
        service="x",
        summary="y",
        confidence=0.5,
        audit_metadata=AuditMetadata(created_at=datetime.now(UTC)),
    )
    assert r.incident_timeline is None


def test_rca_agent_still_renders_from_the_enriched_payload():
    from agents.rca_agent.agent import _render_evidence_block

    r = correlate(CorrelationInput(service="checkout", window=_window()), force_synthetic=True)
    block = _render_evidence_block(r.model_dump(mode="json"))
    assert block and "payment" in block
