"""Shared test fixtures.

The autouse fixture here exists to fix #113 — the full-suite pytest hang.

Earlier tests in the run (notably the HITL UI / approval-flow suites that
boot FastAPI via ``TestClient``) call ``install_default_approver()`` as
part of the app's startup/lifespan hooks. That swaps the gate's approver
from the fail-closed ``_no_approver`` default to a real ``ApprovalRequester``
that waits up to ``AIOPS_HITL_APPROVAL_TIMEOUT`` (600s default) for a
human decision. If the global gate state isn't restored, later tests that
rely on the fail-closed default — `test_hitl_enforcement` and a smoke
test in `test_smoke.py` are the two known cases — block for the full
600s budget instead of failing immediately, stalling the whole suite.

Resetting the gate to ``_no_approver`` at both ends of every test makes
the gate's approver hermetic without forcing every test to write its
own setup/teardown.  Unconditional reset (rather than snapshot/restore)
keeps the fixture simple and side-steps the ordering hazard of trying
to capture and replay whatever the previous test left behind.
"""

from __future__ import annotations

import pytest

# Disable embeddings in the test suite by default (#113).
#
# Multiple agents (``alert_triage``, ``incident_classifier``) lazily
# load an 80MB ``sentence-transformers`` model the first time a method
# that needs embeddings runs. The load is a hefty HTTPS download on
# cold cache and a multi-second mmap on warm cache, blowing past the
# 60s pytest-timeout in either case. The eval harness in particular
# walks every agent and would otherwise pay the load cost per agent.
#
# Each agent already has a documented fallback (rule-based dedup /
# classification) when ``_get_embed_model()`` returns ``None``. We
# pin that fallback by replacing the ``_get_embed_model`` function
# on each agent module at conftest load time so it unconditionally
# returns ``None`` — the package may be installed (test env has the
# embeddings extra) but each agent treats ``None`` as "unavailable"
# and never tries to load. We override the function rather than the
# ``_EMBED_MODEL`` cache sentinel because ``reset_state()`` paths in
# incident_classifier reset ``_EMBED_MODEL = None`` between cases and
# would otherwise re-trigger a load.
#
# Tests that specifically need to exercise the embeddings path
# monkeypatch ``_get_embed_model`` back to a fake (see
# ``test_alert_triage_embedding_persistence``); ``monkeypatch.setattr``
# undoes the override per-test without disturbing this default.
from agents.alert_triage import agent as _alert_triage_agent
from agents.incident_classifier import agent as _incident_classifier_agent
from aiops.policy import get_gate
from aiops.tools.observability import jaeger as _jaeger


def _no_embed_model() -> None:
    return None


_alert_triage_agent._get_embed_model = _no_embed_model
_incident_classifier_agent._get_embed_model = _no_embed_model


@pytest.fixture(autouse=True)
def _hermetic_gate_approver():
    """Reset ``HITLGate._approver`` to the fail-closed default around every test.

    Tests that need a custom approver (e.g. the HITL approval flow suite)
    install one inside their own body; the FastAPI lifespan that fires
    inside ``TestClient(...)`` context managers similarly swaps in an
    ``ApprovalRequester``. Either way, this autouse fixture undoes any
    such change after the test exits so the next test starts from the
    same known-good ``_no_approver`` state.

    Resets at both ends (not just teardown) so a leak from a previous
    test that escaped its own cleanup can't taint the next test's setup.
    """
    gate = get_gate()
    gate.reset_approver()
    try:
        yield
    finally:
        gate.reset_approver()


@pytest.fixture(autouse=True)
def _hermetic_jaeger_circuit():
    """Reset the Jaeger circuit breaker around every test (#113).

    The breaker is module-level process state that survives test
    boundaries. A test that trips it (real socket failure or a mocked
    one) would otherwise short-circuit Jaeger calls in the next 30s of
    tests — including any test that monkeypatches ``httpx.get`` to
    succeed. Reset at both ends so the breaker can't leak in either
    direction.
    """
    _jaeger._reset_circuit_for_tests()
    try:
        yield
    finally:
        _jaeger._reset_circuit_for_tests()
