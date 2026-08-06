"""RCA Agent change correlation (migration Phase 5).

Change history is the signal metrics/logs/traces cannot provide: it supplies
the most common actual cause — someone deployed something. These tests pin the
properties that make it safe to depend on:

  * it goes through the registry, never a direct provider import,
  * it is strictly best-effort — an unconfigured or broken SCM seam degrades
    the evidence but must never fail an RCA,
  * "no commits" is recorded as a real finding, not silently dropped, because
    it argues *against* a deploy-induced cause.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents.rca_agent import agent as rca
from aiops.tools.registry import ToolResult


def _triage(service: str = "order-service") -> dict[str, Any]:
    return {
        "affected_service": service,
        "severity": "Sev-1",
        "alert_summary": "order error rate high",
        "audit_metadata": {"decision_trace": ["alert received"]},
    }


def _commits(n: int = 2) -> list[dict[str, Any]]:
    return [
        {
            "sha": f"abc{i}",
            "date": f"2026-08-03T10:0{i}:00Z",
            "author": "Khushi",
            "message": f"change {i}",
        }
        for i in range(n)
    ]


# ─── evidence gathering ──────────────────────────────────────────────────────


def test_scopes_query_to_the_service_source_path(monkeypatch):
    """Repo-wide queries let the model blame a docs commit for a DB outage."""
    seen: dict[str, Any] = {}

    def fake_call(capability, **kwargs):
        seen["capability"] = capability
        seen.update(kwargs)
        return ToolResult(ok=True, data={"commits": _commits()})

    monkeypatch.setattr(rca.get_registry(), "call", fake_call)

    trace: list[str] = []
    out = rca._fetch_change_evidence("order-service", trace)

    assert seen["capability"] == "scm.commit.history"
    assert seen["path"] == "demo/ecommerce/order-service"
    assert seen["since"].endswith("Z")
    assert len(out) == 2


def test_unmapped_service_falls_back_to_repo_wide(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_call(capability, **kwargs):
        seen.update(kwargs)
        return ToolResult(ok=True, data={"commits": []})

    monkeypatch.setattr(rca.get_registry(), "call", fake_call)

    trace: list[str] = []
    rca._fetch_change_evidence("some-unknown-service", trace)

    assert seen["path"] is None
    assert any("no source path mapped" in line for line in trace)


def test_unregistered_capability_degrades(monkeypatch):
    """CI and token-less runs must still produce an RCA."""

    def fake_call(capability, **kwargs):
        raise KeyError(f"No provider registered for capability {capability!r}")

    monkeypatch.setattr(rca.get_registry(), "call", fake_call)

    trace: list[str] = []
    assert rca._fetch_change_evidence("order-service", trace) is None
    assert any("not registered" in line for line in trace)


def test_seam_error_degrades(monkeypatch):
    monkeypatch.setattr(
        rca.get_registry(),
        "call",
        lambda capability, **kw: ToolResult(ok=False, error="github circuit open"),
    )
    trace: list[str] = []
    assert rca._fetch_change_evidence("order-service", trace) is None
    assert any("circuit open" in line for line in trace)


def test_unexpected_exception_degrades(monkeypatch):
    def boom(capability, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(rca.get_registry(), "call", boom)
    trace: list[str] = []
    assert rca._fetch_change_evidence("order-service", trace) is None
    assert any("RuntimeError" in line for line in trace)


def test_no_commits_is_a_finding_not_a_failure(monkeypatch):
    """'Nothing changed' is evidence against a deploy-induced cause.

    Returning None here would be wrong — None means "we could not look",
    which is a different claim from "we looked and found nothing".
    """
    monkeypatch.setattr(
        rca.get_registry(),
        "call",
        lambda capability, **kw: ToolResult(ok=True, data={"commits": []}),
    )
    trace: list[str] = []
    out = rca._fetch_change_evidence("order-service", trace)
    assert out == [], "empty list, not None"
    assert any("no commits touching" in line for line in trace)


# ─── prompt rendering ────────────────────────────────────────────────────────


def test_render_omits_block_when_unavailable():
    assert rca._render_change_block(None) == ""


def test_render_states_no_commits_explicitly():
    block = rca._render_change_block([])
    assert "no commits" in block


def test_render_includes_sha_date_and_message():
    block = rca._render_change_block(_commits(1))
    assert "abc0" in block and "2026-08-03T10:00:00Z" in block and "change 0" in block


def test_prompt_warns_against_assuming_causation():
    """Without this the model blames whatever commit is newest."""
    block = rca._render_change_block(_commits(1))
    assert "CORRELATION, not proof of causation" in block


def test_user_prompt_carries_change_evidence():
    prompt = rca._render_user_prompt(_triage(), None, _commits(1))
    assert "Recent changes to this service" in prompt
    assert "abc0" in prompt


def test_user_prompt_unchanged_without_change_evidence():
    """Backward compatibility: existing callers pass no change evidence."""
    prompt = rca._render_user_prompt(_triage(), None, None)
    assert "Recent changes to this service" not in prompt


# ─── end to end ──────────────────────────────────────────────────────────────


def test_analyze_survives_a_broken_scm_seam(monkeypatch):
    """The whole point: change correlation is a bonus, never a dependency."""

    def boom(capability, **kwargs):
        raise RuntimeError("scm exploded")

    monkeypatch.setattr(rca.get_registry(), "call", boom)
    verdict = rca.analyze(_triage(), scenario_id="order_service_http_500")
    assert verdict is not None
    assert verdict.ranked_fix_steps is not None


@pytest.mark.parametrize("service", ["user-service", "order-service", "payment-service"])
def test_every_ecommerce_service_has_a_source_path(service):
    """A missing mapping silently widens the query to the whole repo."""
    assert service in rca._SERVICE_SOURCE_PATHS


# ─── flagd mapping must not leak onto the ecommerce SUT ──────────────────────


@pytest.mark.parametrize(
    "service",
    ["user-service", "order-service", "payment-service", "mock-payment-gateway", "frontend"],
)
def test_ecommerce_services_get_no_flagd_flag(service):
    """ecommerce faults are injected with kubectl, not flagd.

    Regression guard: ``_normalise`` strips a trailing "service", so
    "payment-service" collapsed to "payment" and resolved to the OTel Demo's
    ``paymentFailure``. RCA then annotated a fix step with an executable
    action targeting a flag that does not govern the failing service — and
    which stops existing entirely once the OTel Demo is removed.
    """
    from agents.rca_agent.remediation_map import flag_for_service

    assert flag_for_service(service) is None


def test_ecommerce_services_resolve_to_real_failure_keys():
    """Every ecommerce service maps to real, executable failure keys.

    Replaces a test that asserted flagd flags (paymentFailure, cartFailure,
    recommendationCacheFailure) for OTel Demo services. Those services and
    flagd itself were removed in the migration, so the old assertions pinned
    behaviour that could only ever produce an unexecutable fix step.

    flag_for_service now returns None for these — each ecommerce service has
    SEVERAL possible faults, so a service name alone cannot identify one. That
    is deliberate: it forces the agent to reason from evidence instead of a
    name lookup.
    """
    from agents.rca_agent.remediation_map import faults_for_service, flag_for_service

    for svc in ("user-service", "order-service", "payment-service"):
        keys = faults_for_service(svc)
        assert len(keys) >= 3, f"{svc} should have several candidate faults, got {keys}"
        assert flag_for_service(svc) is None, (
            f"{svc} has multiple faults, so no single key can be inferred from the name"
        )

    # Spelling variants collapse to the same service.
    assert faults_for_service("payment") == faults_for_service("payment-service")


def test_failure_keys_are_registered_in_the_injector():
    """The map must not name a key the executor cannot clear."""
    from agents.rca_agent.remediation_map import faults_for_service
    from demo.ecommerce.failure_injection import FAILURES

    for svc in ("user-service", "order-service", "payment-service"):
        for key in faults_for_service(svc):
            assert key in FAILURES, f"{svc}: {key!r} is not a registered failure key"
