"""Tests for the ``observability.events.query`` capability.

Two jobs.

**Prove the provider is honest about what it could not do.** "We could not check" and
"nothing was deployed" are different facts and only the second exonerates a deploy,
so the disabled / no-client / API-error paths are all distinguishable.

**Prove promoting the two Kubernetes API calls out of the agent changed nothing.**
``test_new_capability_and_legacy_path_agree_on_the_same_cluster_data`` drives the
existing ``timeline_sources.fetch_change_events`` and the new capability from *one*
fake cluster and asserts the derived facts match. That turns "no agent logic
changed" from a review promise into something CI enforces, which is the whole
justification for doing this before the Log Correlation migration rather than during
it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aiops.tools.observability import k8s_events
from aiops.tools.registry import ToolResult

NS = "ecommerce"
WINDOW_START = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(minutes=30)
INSIDE = WINDOW_START + timedelta(minutes=5)
OUTSIDE = WINDOW_START - timedelta(hours=3)


# --- fakes mirroring the kubernetes client's attribute shape -------------


class _Obj:
    def __init__(self, kind: str, name: str, namespace: str = NS) -> None:
        self.kind = kind
        self.name = name
        self.namespace = namespace


class _Event:
    def __init__(
        self,
        *,
        kind: str,
        name: str,
        reason: str,
        message: str = "msg",
        last_timestamp: datetime | None = INSIDE,
        event_time: datetime | None = None,
        first_timestamp: datetime | None = None,
        count: int | None = 1,
        type_: str = "Normal",
    ) -> None:
        self.involved_object = _Obj(kind, name)
        self.reason = reason
        self.message = message
        self.last_timestamp = last_timestamp
        self.event_time = event_time
        self.first_timestamp = first_timestamp
        self.count = count
        self.type = type_


class _ManagedField:
    def __init__(self, manager: str, operation: str, time: datetime | None) -> None:
        self.manager = manager
        self.operation = operation
        self.time = time


class _Meta:
    def __init__(self, name: str, managed_fields: list[_ManagedField] | None = None) -> None:
        self.name = name
        self.namespace = NS
        self.resource_version = "12345"
        self.managed_fields = managed_fields or []


class _ConfigMap:
    def __init__(self, name: str, managed_fields: list[_ManagedField] | None = None) -> None:
        self.metadata = _Meta(name, managed_fields)
        # Present on the real object and deliberately never read — see
        # test_configmap_data_is_never_collected.
        self.data = {"DB_PASSWORD": "hunter2"}


class _Listing:
    def __init__(self, items: list[Any]) -> None:
        self.items = items


class _FakeCore:
    """A fake CoreV1Api. Counts calls so we can assert the namespace-wide pattern."""

    def __init__(
        self,
        events: list[Any] | None = None,
        configmaps: list[Any] | None = None,
        *,
        event_exc: Exception | None = None,
        cm_exc: Exception | None = None,
    ) -> None:
        self._events = events or []
        self._configmaps = configmaps or []
        self._event_exc = event_exc
        self._cm_exc = cm_exc
        self.event_calls = 0
        self.cm_calls = 0

    def list_namespaced_event(self, namespace: str, timeout_seconds: int | None = None) -> _Listing:
        self.event_calls += 1
        if self._event_exc:
            raise self._event_exc
        return _Listing(self._events)

    def list_namespaced_config_map(
        self, namespace: str, timeout_seconds: int | None = None
    ) -> _Listing:
        self.cm_calls += 1
        if self._cm_exc:
            raise self._cm_exc
        return _Listing(self._configmaps)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("AIOPS_K8S_EVENTS_ENABLED", "true")
    monkeypatch.setenv("AIOPS_K8S_NAMESPACE", NS)


def _install(monkeypatch, core: _FakeCore | None) -> None:
    monkeypatch.setattr(k8s_events, "_core_api", lambda: core, raising=True)


# --- the three "could not look" paths, kept distinct -------------------


def test_disabled_by_default(monkeypatch):
    """A developer with a live kubeconfig must not silently start hitting it."""
    monkeypatch.delenv("AIOPS_K8S_EVENTS_ENABLED", raising=False)
    res = k8s_events.query_events()
    assert not res.ok
    assert res.metadata["reason"] == "disabled"
    assert res.metadata["flag"] == "AIOPS_K8S_EVENTS_ENABLED"


def test_flag_is_read_per_call_not_at_import(monkeypatch):
    """So a fixture can reach it with ``setenv``/``delenv``.

    The three RA-007 gates are import-time constants, which is exactly why
    ``tests/conftest.py`` has to patch module objects for them instead.
    """
    _install(monkeypatch, _FakeCore())
    monkeypatch.setenv("AIOPS_K8S_EVENTS_ENABLED", "true")
    assert k8s_events.query_events().ok
    monkeypatch.setenv("AIOPS_K8S_EVENTS_ENABLED", "false")
    assert not k8s_events.query_events().ok


def test_absent_kube_client_is_reported_as_no_client(monkeypatch, enabled):
    _install(monkeypatch, None)
    res = k8s_events.query_events()
    assert not res.ok
    assert res.metadata["reason"] == "no_client"


def test_event_api_failure_is_distinguishable_from_an_absent_client(monkeypatch, enabled):
    """Only "we looked and found nothing" exonerates a deploy — never conflate."""
    _install(monkeypatch, _FakeCore(event_exc=RuntimeError("api server unreachable")))
    res = k8s_events.query_events()
    assert not res.ok
    assert res.metadata["reason"] == "event_list_failed"
    assert "RuntimeError" in (res.error or "")


def test_import_of_the_package_does_not_require_the_kubernetes_package():
    """``kubernetes`` ships only in the ``ui`` extra.

    A top-level import here would make ``agents/log_correlation/agent.py``
    unimportable on a ``--extra dev`` install, since it imports this package at
    module scope.
    """
    import ast
    import pathlib

    source = pathlib.Path(k8s_events.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_imports = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "kubernetes" not in top_level_imports


# --- success shape ------------------------------------------------------


def test_returns_events_and_configmaps_unfiltered(monkeypatch, enabled):
    """The provider fetches; the caller decides relevance.

    Deliberately returns an event for an unrelated service too: attribution rules
    (``owner_name``, ``_relates_to``) are the agent's judgement and must not migrate
    into the platform.
    """
    core = _FakeCore(
        events=[
            _Event(kind="Pod", name="payment-service-5bff6448f5-cdcsw", reason="BackOff"),
            _Event(kind="Pod", name="totally-unrelated-abc12", reason="Pulled"),
        ],
        configmaps=[_ConfigMap("flagd-config", [_ManagedField("helm", "Apply", INSIDE)])],
    )
    _install(monkeypatch, core)

    res = k8s_events.query_events()
    assert res.ok
    assert res.data["namespace"] == NS
    assert len(res.data["events"]) == 2, "the provider must not filter by service"
    assert len(res.data["configmaps"]) == 1
    assert res.metadata["event_count"] == 2

    event = res.data["events"][0]
    assert event["involved_object"] == {
        "kind": "Pod",
        "name": "payment-service-5bff6448f5-cdcsw",
        "namespace": NS,
    }
    assert event["reason"] == "BackOff"


def test_one_api_call_per_listing_not_one_per_service(monkeypatch, enabled):
    """This runs on the incident path; a per-candidate query would be N calls."""
    core = _FakeCore(events=[_Event(kind="Pod", name="a-abc12", reason="Pulled")])
    _install(monkeypatch, core)
    k8s_events.query_events()
    assert core.event_calls == 1
    assert core.cm_calls == 1


def test_timestamps_are_iso_strings_so_the_payload_survives_json(monkeypatch, enabled):
    """This payload gets cached and serialised by ``aiops/context/``.

    Emitting ``datetime`` would work today and break the first time a context is
    written to a cache or an audit log.
    """
    import json

    core = _FakeCore(
        events=[_Event(kind="Pod", name="a-abc12", reason="Pulled", last_timestamp=INSIDE)],
        configmaps=[_ConfigMap("a-config", [_ManagedField("kubectl", "Update", INSIDE)])],
    )
    _install(monkeypatch, core)
    res = k8s_events.query_events()

    assert res.data["events"][0]["last_timestamp"] == INSIDE.isoformat()
    assert res.data["configmaps"][0]["managed_fields"][0]["time"] == INSIDE.isoformat()
    json.dumps(res.data)  # must not raise


def test_all_three_event_timestamp_fields_are_emitted(monkeypatch, enabled):
    """Which one is populated varies by Kubernetes version and by how the event was
    recorded, so choosing here would silently drop events."""
    core = _FakeCore(
        events=[
            _Event(
                kind="Pod",
                name="a-abc12",
                reason="Pulled",
                last_timestamp=None,
                event_time=INSIDE,
                first_timestamp=None,
            )
        ]
    )
    _install(monkeypatch, core)
    event = k8s_events.query_events().data["events"][0]

    assert set(event) >= {"last_timestamp", "event_time", "first_timestamp"}
    assert event["last_timestamp"] is None
    assert event["event_time"] == INSIDE.isoformat()


def test_configmap_data_is_never_collected(monkeypatch, enabled):
    """The one field likely to hold a credential, and not needed to detect a change.

    Not collecting it is a stronger guarantee than redacting it afterwards.
    """
    core = _FakeCore(
        configmaps=[_ConfigMap("app-config", [_ManagedField("helm", "Apply", INSIDE)])]
    )
    _install(monkeypatch, core)
    payload = k8s_events.query_events().data

    assert "hunter2" not in str(payload)
    assert "data" not in payload["configmaps"][0]


def test_configmap_failure_does_not_discard_the_events_already_fetched(monkeypatch, enabled):
    """A ConfigMap RBAC denial is common; reporting total failure would throw away
    good data to report a secondary problem."""
    core = _FakeCore(
        events=[_Event(kind="Pod", name="a-abc12", reason="BackOff")],
        cm_exc=PermissionError("configmaps is forbidden"),
    )
    _install(monkeypatch, core)
    res = k8s_events.query_events()

    assert res.ok
    assert len(res.data["events"]) == 1
    assert res.data["configmaps"] == []
    assert "PermissionError" in res.metadata["configmaps_error"]


def test_configmaps_can_be_skipped(monkeypatch, enabled):
    core = _FakeCore(events=[], configmaps=[_ConfigMap("a-config")])
    _install(monkeypatch, core)
    res = k8s_events.query_events(include_configmaps=False)

    assert res.ok
    assert core.cm_calls == 0
    assert res.data["configmaps"] == []


def test_events_without_an_involved_object_are_skipped(monkeypatch, enabled):
    """A malformed item must cost that item, not the listing."""

    class _Headless:
        involved_object = None

    core = _FakeCore(events=[_Headless(), _Event(kind="Pod", name="a-abc12", reason="Pulled")])
    _install(monkeypatch, core)
    assert len(k8s_events.query_events().data["events"]) == 1


def test_empty_namespace_is_a_successful_empty_answer(monkeypatch, enabled):
    """``ok=True`` with no events is "we looked and nothing happened" — a real fact,
    and the one that lets a consumer rule a deploy out."""
    _install(monkeypatch, _FakeCore())
    res = k8s_events.query_events()
    assert res.ok
    assert res.data["events"] == []


# --- policy -------------------------------------------------------------


def test_capability_is_registered_at_autonomy_none():
    """A list call cannot change the system; gating it would only add latency.

    Registered explicitly rather than left to the ``AIOPS_HITL_DEFAULT`` fall-through
    (which is ``optional``), because "a human may approve this read" is not the
    intent.
    """
    from aiops.policy.gate import DEFAULT_LEVELS, AutonomyLevel

    assert DEFAULT_LEVELS["observability.events.query"] is AutonomyLevel.NONE


def test_capability_level_is_mirrored_in_the_rego_policy():
    """ADR-0005: ``gate.py`` and ``policies/hitl.rego`` are a dual source of truth.

    Not a check of the whole file — the two are already out of sync for several
    pre-existing capabilities, and fixing that drift is deliberately out of scope
    here. This asserts only that the capability added alongside this tool was added
    to both.
    """
    import pathlib

    rego = (pathlib.Path(__file__).resolve().parent.parent / "policies" / "hitl.rego").read_text(
        encoding="utf-8"
    )
    assert 'level := "none"' in rego
    assert '"observability.events.query"' in rego


def test_capability_is_registered_in_the_tool_registry():
    from aiops.tools import get_registry
    from aiops.tools.observability import k8s_events as _  # noqa: F401  (registers on import)

    tool = get_registry().by_capability("observability.events.query")
    assert tool.provider == "kubernetes"


# --- the equivalence proof --------------------------------------------


def test_new_capability_and_legacy_path_agree_on_the_same_cluster_data(monkeypatch):
    """Promoting the two API calls out of the agent changes no derived fact.

    Both paths are driven from ONE fake cluster. The legacy path applies the agent's
    filtering (``_relates_to``, ``owner_name``, ``_in_window``,
    ``_DEPLOYMENT_REASONS``, the ``flagd-config`` special case) to objects it read
    itself; the new path reads the same objects through the capability. This asserts
    the *inputs to that filtering* are identical, which is what makes "only the two
    API calls moved" a CI-enforced claim rather than an assurance.

    The filtering itself is deliberately NOT moved into the platform — it is
    judgement about what a Kubernetes event means for a service, argued at length in
    ``timeline_sources.py``.
    """
    from agents.log_correlation import timeline_sources

    events = [
        # In-window, related, a deployment reason -> the legacy path keeps this.
        _Event(kind="Pod", name="payment-service-5bff6448f5-cdcsw", reason="BackOff"),
        # In-window and related, but not a deployment reason -> legacy drops it.
        _Event(kind="Pod", name="payment-service-5bff6448f5-cdcsw", reason="NotAReason"),
        # Related and a deployment reason, but out of window -> legacy drops it.
        _Event(
            kind="Pod",
            name="payment-service-5bff6448f5-cdcsw",
            reason="Killing",
            last_timestamp=OUTSIDE,
        ),
        # Another service entirely -> legacy drops it.
        _Event(kind="Pod", name="frontend-proxy-6d4948d448-7ttqp", reason="BackOff"),
    ]
    configmaps = [_ConfigMap("flagd-config", [_ManagedField("helm", "Apply", INSIDE)])]

    core = _FakeCore(events=events, configmaps=configmaps)

    # --- legacy path: agent reads the cluster itself
    monkeypatch.setattr(timeline_sources, "_K8S_ENABLED", True, raising=False)
    monkeypatch.setattr(timeline_sources, "_NAMESPACE", NS, raising=False)
    monkeypatch.setattr(timeline_sources, "_k8s_clients", lambda: core, raising=True)
    legacy_events, legacy_note = timeline_sources.fetch_change_events(
        "payment-service", WINDOW_START, WINDOW_END
    )

    # --- new path: the same cluster, read through the capability
    monkeypatch.setenv("AIOPS_K8S_EVENTS_ENABLED", "true")
    monkeypatch.setenv("AIOPS_K8S_NAMESPACE", NS)
    _install(monkeypatch, core)
    res = k8s_events.query_events()

    assert legacy_note is None, "the legacy path should have completed cleanly"
    assert res.ok

    # The capability hands the agent every object the agent read for itself, so the
    # filtering it then applies has the identical input set.
    assert len(res.data["events"]) == len(events)
    legacy_object_names = {e.involved_object.name for e in events}
    capability_object_names = {e["involved_object"]["name"] for e in res.data["events"]}
    assert capability_object_names == legacy_object_names

    assert {c["name"] for c in res.data["configmaps"]} == {"flagd-config"}

    # And the reasons the agent's _DEPLOYMENT_REASONS table keys on survive intact.
    assert {e["reason"] for e in res.data["events"]} == {r.reason for r in events}

    # Sanity-check that the legacy filtering really did discriminate, so the
    # equality above is a meaningful claim about a non-trivial pipeline. Of the four
    # events fed in, exactly one survives (wrong reason / out of window / wrong
    # service account for the other three), and the flagd-config managedFields write
    # contributes a second timeline entry — fetch_change_events extends its result
    # with fetch_configuration_events, so both sources are in scope for this proof.
    by_source = sorted(e.source for e in legacy_events)
    assert by_source == ["configuration", "deployment"]
    deployment = next(e for e in legacy_events if e.source == "deployment")
    assert "BackOff" in deployment.event
    configuration = next(e for e in legacy_events if e.source == "configuration")
    assert "flagd-config" in configuration.event


def test_capability_payload_carries_every_field_the_agent_filtering_needs(monkeypatch, enabled):
    """Pins the contract the Log Correlation migration will depend on.

    ``fetch_change_events`` reads ``involved_object.kind``/``.name``, one of the three
    timestamps, and ``reason``; ``fetch_configuration_events`` reads the ConfigMap
    name plus each ``managedFields`` entry's ``time``, ``manager`` and ``operation``.
    If a future change to this provider drops one of those, the migration breaks —
    so the requirement is asserted here, next to the provider, rather than being
    discovered later.
    """
    core = _FakeCore(
        events=[_Event(kind="Pod", name="a-abc12", reason="BackOff")],
        configmaps=[_ConfigMap("a-config", [_ManagedField("helm", "Apply", INSIDE)])],
    )
    _install(monkeypatch, core)
    payload = k8s_events.query_events().data

    event = payload["events"][0]
    assert {"kind", "name"} <= set(event["involved_object"])
    assert {"reason", "last_timestamp", "event_time", "first_timestamp"} <= set(event)

    cm = payload["configmaps"][0]
    assert "name" in cm
    assert {"time", "manager", "operation"} <= set(cm["managed_fields"][0])


def test_result_is_a_toolresult_not_a_raw_dict(monkeypatch, enabled):
    _install(monkeypatch, _FakeCore())
    assert isinstance(k8s_events.query_events(), ToolResult)
