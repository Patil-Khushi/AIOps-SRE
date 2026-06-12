"""Tests for the ServiceNow resolved-ticket watcher (PRS-007, Step 1).

The watcher is driven through ``poll_once`` (sync, side-effect-complete) with a
fake ``itsm_call`` and a fake ``synthesize`` so no event loop, no real
ServiceNow, and no LLM are involved.
"""

from __future__ import annotations

import pytest

from agents.knowledge_synthesizer.snow_watcher import _SnowWatcher
from aiops import state as state_pkg
from aiops.state import repository as repo


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("AIOPS_STATE_DB_URL", "sqlite:///:memory:")
    state_pkg.reset_engine_for_tests()
    state_pkg.init_db()
    yield
    state_pkg.reset_engine_for_tests()


def _incident(number: str, updated: str, **extra) -> dict:
    base = {
        "number": number,
        "sys_id": f"sys-{number}",
        "state": "6",
        "sys_updated_on": updated,
        "resolved_at": updated,
        "opened_at": "2026-06-12 09:00:00",
        "short_description": f"{number} payment errors",
        "cmdb_ci": {"display_value": "payment"},
    }
    base.update(extra)
    return base


class FakeItsm:
    """Records calls; returns queued incident batches as a ToolResult-like obj."""

    class _Res:
        def __init__(self, ok=True, data=None, error=None):
            self.ok = ok
            self.data = data or {}
            self.error = error

    def __init__(self, incidents):
        self._incidents = incidents
        self.calls: list[str] = []
        self.fail = False

    def __call__(self, query, fields, limit):
        self.calls.append(query)
        if self.fail:
            return self._Res(ok=False, error="boom")
        # "ORDERBYDESC" = the first-run newest-1 probe.
        if "ORDERBYDESC" in query:
            newest = max(self._incidents, key=lambda i: i["sys_updated_on"], default=None)
            return self._Res(data={"incidents": [newest] if newest else []})
        # Otherwise return everything strictly after the checkpoint in the query.
        cp = ""
        if "sys_updated_on>" in query:
            cp = query.split("sys_updated_on>", 1)[1].split("^", 1)[0]
        rows = [i for i in self._incidents if i["sys_updated_on"] > cp]
        rows.sort(key=lambda i: i["sys_updated_on"])
        return self._Res(data={"incidents": rows[:limit]})


def _watcher(itsm, synth, tmp_path):
    return _SnowWatcher(
        interval_seconds=45,
        itsm_call=itsm,
        synthesize=synth,
        state_file=tmp_path / "watch.json",
    )


# ─── happy path ──────────────────────────────────────────────────────────────


def test_new_resolved_ticket_triggers_synthesis(tmp_path):
    calls: list[dict] = []

    def synth(bundle):
        calls.append(bundle)
        # mimic agent.run persisting a KB row so the ledger reflects it
        aid = repo.save_kb_article(
            title="pm", body="b", incident_id=bundle["incident_id"], service="payment"
        )
        return {"kb_article_id": aid, "dedup_action": "create"}

    itsm = FakeItsm([_incident("INC001", "2026-06-12 10:00:00")])
    w = _watcher(itsm, synth, tmp_path)

    # First poll only anchors the checkpoint — processes nothing.
    first = w.poll_once()
    assert first["initialized"] is True
    assert len(calls) == 0

    # A newer ticket resolves; next poll synthesizes it.
    itsm._incidents.append(_incident("INC002", "2026-06-12 10:05:00"))
    second = w.poll_once()
    assert second["processed"] == 1
    assert calls[0]["incident_id"] == "INC002"
    assert calls[0]["rca_verdict"]["affected_service"] == "payment"
    # ticket_only source tagged on the KB row (no RCA on record)
    art = repo.find_kb_by_incident_id("INC002")
    assert art["audit_metadata"].get("source") == "ticket_only"


def test_uses_stored_rca_when_present(tmp_path):
    repo.save_rca_result(
        incident_id="INC100",
        affected_service="cart",
        verdict={
            "affected_service": "cart",
            "root_cause": "flagd cartFailure on",
            "ranked_fix_steps": [
                {"description": "flip off", "blast_radius": "low", "rollback": "on"}
            ],
            "confidence_score": 0.9,
        },
    )
    seen: list[dict] = []

    def synth(bundle):
        seen.append(bundle)
        return {"kb_article_id": None, "dedup_action": "create"}

    itsm = FakeItsm([])
    w = _watcher(itsm, synth, tmp_path)
    w.poll_once()  # init (no incidents)
    itsm._incidents.append(
        _incident("INC100", "2026-06-12 11:00:00", cmdb_ci={"display_value": "cart"})
    )
    w.poll_once()
    assert seen[0]["rca_verdict"]["root_cause"] == "flagd cartFailure on"


# ─── idempotency ─────────────────────────────────────────────────────────────


def test_already_ledgered_ticket_is_skipped(tmp_path):
    # Pre-seed a KB article for the incident → the ledger should skip it.
    repo.save_kb_article(title="pm", body="b", incident_id="INC777", service="payment")
    calls: list[dict] = []
    itsm = FakeItsm([])
    w = _watcher(itsm, lambda b: calls.append(b) or {"kb_article_id": 1}, tmp_path)
    w.poll_once()  # init
    itsm._incidents.append(_incident("INC777", "2026-06-12 12:00:00"))
    res = w.poll_once()
    assert res["processed"] == 0
    assert calls == []


def test_does_not_resynthesize_across_polls(tmp_path):
    n = {"count": 0}

    def synth(bundle):
        n["count"] += 1
        aid = repo.save_kb_article(
            title="pm", body="b", incident_id=bundle["incident_id"], service="payment"
        )
        return {"kb_article_id": aid}

    itsm = FakeItsm([])
    w = _watcher(itsm, synth, tmp_path)
    w.poll_once()  # init
    itsm._incidents.append(_incident("INC888", "2026-06-12 13:00:00"))
    w.poll_once()  # synthesizes once
    w.poll_once()  # checkpoint advanced past it → not rescanned
    assert n["count"] == 1


# ─── resilience ──────────────────────────────────────────────────────────────


def test_itsm_failure_does_not_raise(tmp_path):
    itsm = FakeItsm([_incident("INC001", "2026-06-12 10:00:00")])
    w = _watcher(itsm, lambda b: {"kb_article_id": 1}, tmp_path)
    w.poll_once()  # init succeeds
    itsm.fail = True
    res = w.poll_once()  # must not raise
    assert "error" in res
    assert w.status()["consecutive_failures"] == 1


def test_circuit_breaker_backs_off_after_five_failures(tmp_path):
    itsm = FakeItsm([])
    w = _watcher(itsm, lambda b: {"kb_article_id": 1}, tmp_path)
    w.poll_once()  # init
    itsm.fail = True
    for _ in range(5):
        w.poll_once()
    st = w.status()
    assert st["consecutive_failures"] >= 5
    assert st["backed_off"] is True
    assert st["poll_interval_seconds"] == 300.0


# ─── checkpoint ──────────────────────────────────────────────────────────────


def test_checkpoint_advances_and_persists(tmp_path):
    def synth(bundle):
        aid = repo.save_kb_article(
            title="pm", body="b", incident_id=bundle["incident_id"], service="payment"
        )
        return {"kb_article_id": aid}

    itsm = FakeItsm([_incident("INC001", "2026-06-12 10:00:00")])
    state = tmp_path / "watch.json"
    w = _SnowWatcher(interval_seconds=45, itsm_call=itsm, synthesize=synth, state_file=state)
    w.poll_once()  # init → checkpoint = 10:00:00 (newest existing)
    assert w.status()["checkpoint"] == "2026-06-12 10:00:00"

    itsm._incidents.append(_incident("INC002", "2026-06-12 10:30:00"))
    w.poll_once()
    assert w.status()["checkpoint"] == "2026-06-12 10:30:00"

    # A fresh watcher loads the persisted checkpoint (no re-scan/init).
    w2 = _SnowWatcher(interval_seconds=45, itsm_call=itsm, synthesize=synth, state_file=state)
    assert w2.status()["checkpoint"] == "2026-06-12 10:30:00"
    res = w2.poll_once()
    assert res.get("initialized") is None  # already initialized
    assert res["processed"] == 0  # nothing newer than the checkpoint
