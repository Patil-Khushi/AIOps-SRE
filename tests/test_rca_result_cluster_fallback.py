"""RCA verdict persistence for Suppressed alerts (no real ServiceNow ticket).

Context: ``save_rca_result`` only ever fires when a request carries a real
ServiceNow ``incident_id`` (``demo/ui/server.py``'s RCA endpoint). Auto-
Ticketing (``agents/auto_ticketing/agent.py::ticket``) deliberately skips
ticket creation for a ``Suppressed`` triage verdict, so that verdict never
gets an ``incident_id`` — and its RCA result was previously unreachable to
Historical Incident RAG forever, even though the same failure cluster
recurring is exactly the kind of thing "have we seen this before?" should
answer.

``aiops.state.repository.save_rca_result_for_cluster`` fixes this by
persisting under a synthetic identity derived from the Alert Triage dedup
cluster (``Alert.cluster_key()``), never a ServiceNow incident id — see
``CLUSTER_INCIDENT_PREFIX``'s docstring for the format contract this file
enforces.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agents.alert_triage.models import AuditMetadata, TriageVerdict
from agents.auto_ticketing import ticket
from aiops.state import repository as state_repo


@pytest.fixture()
def clean_rca_results():
    state_repo.delete_all_rca_results()
    yield
    state_repo.delete_all_rca_results()


def _verdict(*, status: str, cluster_key: str, duplicate_alert_count: int = 1) -> TriageVerdict:
    return TriageVerdict(
        affected_service="order-service",
        severity="Sev-2",
        confidence_score=0.9,
        alert_summary="order-service error rate above threshold",
        assigned_team="Order Experience",
        duplicate_alert_count=duplicate_alert_count,
        status=status,
        audit_metadata=AuditMetadata(created_at=datetime(2026, 8, 17, 9, 0, tzinfo=UTC)),
        cluster_key=cluster_key,
    )


def _rca_payload(**overrides) -> dict:
    payload = {
        "affected_service": "order-service",
        "root_cause": "order-service is returning HTTP 500 on the order-creation path",
        "root_cause_status": "confirmed",
        "confidence_score": 0.82,
    }
    payload.update(overrides)
    return payload


# ─── 1: Active alert with a real incident_id persists normally ─────────────


def test_active_alert_with_real_incident_id_persists_normally(clean_rca_results):
    verdict = _verdict(status="Active", cluster_key="deadbeef00000001")
    assert verdict.status == "Active"

    row_id = state_repo.save_rca_result(
        incident_id="INC0012345", verdict=_rca_payload(), affected_service="order-service"
    )
    assert row_id > 0

    stored = state_repo.get_rca_result("INC0012345")
    assert stored is not None
    assert stored["incident_id"] == "INC0012345"
    assert not stored["incident_id"].startswith(state_repo.CLUSTER_INCIDENT_PREFIX)


# ─── 2: Suppressed alert without incident_id persists via cluster identity ──


def test_suppressed_alert_without_incident_id_persists_using_cluster_identity(clean_rca_results):
    verdict = _verdict(status="Suppressed", cluster_key="deadbeef00000002")
    assert verdict.incident_id is None
    assert verdict.cluster_key == "deadbeef00000002"

    row_id = state_repo.save_rca_result_for_cluster(
        cluster_key=verdict.cluster_key,
        verdict=_rca_payload(),
        affected_service="order-service",
    )
    assert row_id > 0

    synthetic_id = f"{state_repo.CLUSTER_INCIDENT_PREFIX}deadbeef00000002"
    stored = state_repo.get_rca_result(synthetic_id)
    assert stored is not None
    assert stored["incident_id"] == synthetic_id
    assert stored["affected_service"] == "order-service"


# ─── 3: Repeated suppressed alerts for the same cluster do not duplicate ────


def test_repeated_suppressed_alerts_same_cluster_do_not_create_uncontrolled_duplicates(
    clean_rca_results,
):
    cluster_key = "deadbeef00000003"
    for i in range(5):
        state_repo.save_rca_result_for_cluster(
            cluster_key=cluster_key,
            verdict=_rca_payload(root_cause=f"occurrence {i}"),
            affected_service="order-service",
        )

    synthetic_id = f"{state_repo.CLUSTER_INCIDENT_PREFIX}{cluster_key}"
    all_rows = state_repo.list_rca_results(limit=200)
    matching = [r for r in all_rows if r["incident_id"] == synthetic_id]
    assert len(matching) == 1, "one cluster must collapse to exactly one row, not accumulate"

    # And it's the freshest content that survives, not the first.
    stored = state_repo.get_rca_result(synthetic_id)
    assert stored["verdict"]["root_cause"] == "occurrence 4"


def test_different_clusters_of_the_same_service_stay_distinct(clean_rca_results):
    """The upsert key is the cluster, not the service — two different failure
    clusters on the same service must not collide."""
    state_repo.save_rca_result_for_cluster(
        cluster_key="cluster-a", verdict=_rca_payload(root_cause="cause A"), affected_service="s"
    )
    state_repo.save_rca_result_for_cluster(
        cluster_key="cluster-b", verdict=_rca_payload(root_cause="cause B"), affected_service="s"
    )
    all_rows = state_repo.list_rca_results(limit=200)
    ids = {r["incident_id"] for r in all_rows}
    assert f"{state_repo.CLUSTER_INCIDENT_PREFIX}cluster-a" in ids
    assert f"{state_repo.CLUSTER_INCIDENT_PREFIX}cluster-b" in ids


# ─── 4: RAG can retrieve a previous suppressed-alert RCA when similar ──────


def test_rag_can_retrieve_a_previous_suppressed_alert_rca_when_genuinely_similar(
    clean_rca_results, monkeypatch
):
    from agents.rca_agent import incident_rag

    cluster_key = "deadbeef00000004"
    state_repo.save_rca_result_for_cluster(
        cluster_key=cluster_key,
        verdict=_rca_payload(
            root_cause="order-service dependency unavailable",
            investigation={
                "selected_hypothesis_id": "hid-1",
                "matrices": [
                    {"hypothesis": {"hypothesis_id": "hid-1", "category": "dependency_unavailable"}}
                ],
            },
        ),
        affected_service="order-service",
    )

    class _FakeModel:
        def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
            # Every text maps to the same vector — a perfect (1.0) match,
            # well above the retrieval floor, regardless of exact wording.
            return [(1.0, 0.0) for _ in texts]

    import aiops.tools.incident_history.providers.embedding as embed_mod

    monkeypatch.setattr(embed_mod, "get_shared_model", lambda: _FakeModel())

    matches = incident_rag.search_similar_incidents(
        service="order-service",
        summary="order-service dependency unavailable again",
        category="dependency_unavailable",
    )
    assert len(matches) == 1
    assert matches[0].incident_id == f"{state_repo.CLUSTER_INCIDENT_PREFIX}{cluster_key}"


# ─── 5: Synthetic cluster identity is never sent to ServiceNow ─────────────


def test_synthetic_cluster_identity_is_never_sent_to_servicenow(clean_rca_results, monkeypatch):
    """save_rca_result_for_cluster is a pure state.db write — it must never
    reach the tool registry (and therefore never reach itsm.incident.*)."""
    calls: list[tuple[str, dict]] = []

    def _spy_call(capability, **kwargs):
        calls.append((capability, kwargs))
        raise AssertionError(f"unexpected registry call: {capability}")

    import aiops.tools as tools_mod

    real_get_registry = tools_mod.get_registry

    class _SpyRegistry:
        def call(self, capability, **kwargs):
            return _spy_call(capability, **kwargs)

        def __getattr__(self, name):
            return getattr(real_get_registry(), name)

    monkeypatch.setattr(tools_mod, "get_registry", lambda: _SpyRegistry())

    state_repo.save_rca_result_for_cluster(
        cluster_key="deadbeef00000005",
        verdict=_rca_payload(),
        affected_service="order-service",
    )
    assert calls == [], "save_rca_result_for_cluster must never call the tool registry"


def test_ticket_function_never_receives_or_creates_a_cluster_prefixed_incident_id(
    clean_rca_results,
):
    """Auto-Ticketing's own Suppressed-skip path is unaffected: it never sees
    (and cannot produce) a CLUSTER_INCIDENT_PREFIX-shaped id."""
    verdict = _verdict(status="Suppressed", cluster_key="deadbeef00000006", duplicate_alert_count=3)
    record = ticket(verdict)
    assert record.created is False
    assert record.ticket_id is None
    assert "skipped: status=Suppressed" in record.audit_metadata


# ─── 6: Existing RCA scoring / HITL behavior is unchanged ──────────────────


def test_persisted_verdict_content_round_trips_unchanged(clean_rca_results):
    """save_rca_result_for_cluster is a pure store — it must not mutate
    confidence_score or root_cause_status on the way in."""
    payload = _rca_payload(confidence_score=0.67, root_cause_status="probable")
    state_repo.save_rca_result_for_cluster(
        cluster_key="deadbeef00000007", verdict=payload, affected_service="order-service"
    )
    stored = state_repo.get_rca_result(f"{state_repo.CLUSTER_INCIDENT_PREFIX}deadbeef00000007")
    assert stored["verdict"]["confidence_score"] == 0.67
    assert stored["verdict"]["root_cause_status"] == "probable"


def test_active_verdicts_still_create_real_tickets_unaffected_by_this_change(clean_rca_results):
    """Regression: the cluster_key field is purely additive on TriageVerdict —
    an Active verdict's existing ticket-creation path must be untouched."""
    from aiops.tools import get_registry, mock_providers  # noqa: F401

    get_registry().select_provider("itsm.incident.create", "mock.itsm.incident.create")
    verdict = _verdict(status="Active", cluster_key="deadbeef00000008")
    record = ticket(verdict)
    assert record.created is True
