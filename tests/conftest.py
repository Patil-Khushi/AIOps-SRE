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

Snapshotting and restoring per-test makes the gate's approver hermetic
without forcing every test to write its own setup/teardown.
"""

from __future__ import annotations

import pytest

from aiops.policy import get_gate
from aiops.policy.gate import _no_approver


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
    gate.set_approver(_no_approver)
    try:
        yield
    finally:
        gate.set_approver(_no_approver)
