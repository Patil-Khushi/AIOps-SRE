"""Tests for the Resolution Verifier engine (PRS-007 Step 2, increment 2a).

Dependency-injected: a fake ``itsm_call`` records work notes; a fake ``checks``
function returns controlled results; ``sleep_fn`` is a no-op and windows are
zeroed so there's no real stabilization wait.
"""

from __future__ import annotations

from agents.resolution_verifier.models import CheckResult, CheckStatus
from agents.resolution_verifier.verifier import Verifier, VerifyContext


class FakeItsm:
    class _Res:
        def __init__(self, ok=True, data=None, error=None):
            self.ok = ok
            self.data = data or {}
            self.error = error

    def __init__(self, sys_id="sys-INC1"):
        self._sys_id = sys_id
        self.work_notes: list[str] = []
        self.updates: list[dict] = []

    def __call__(self, capability, **kwargs):
        if capability == "itsm.incident.get":
            return self._Res(
                data={"incident": {"number": kwargs.get("number"), "sys_id": self._sys_id}}
            )
        if capability == "itsm.incident.update":
            fields = kwargs.get("fields") or {}
            self.updates.append({"sys_id": kwargs.get("sys_id"), "fields": fields})
            if "work_notes" in fields:
                self.work_notes.append(fields["work_notes"])
            return self._Res(data={"sys_id": kwargs.get("sys_id")})
        return self._Res(ok=False, error=f"unexpected capability {capability}")


def _ctx(incident="INC1", service="payment"):
    return VerifyContext(incident_id=incident, service=service, metric_query="x", threshold=1.0)


def _verifier(itsm, checks, tmp_path):
    return Verifier(
        itsm_call=itsm,
        checks=checks,
        windows=[0.0, 0.0, 0.0],
        sleep_fn=lambda _s: None,
        state_file=tmp_path / "verifier.json",
        metrics_call=lambda q: None,
    )


def _checks(*results):
    return lambda ctx, metrics_call: list(results)


# ─── pass path ───────────────────────────────────────────────────────────────


def test_pass_writes_proof_and_records(tmp_path):
    itsm = FakeItsm()
    checks = _checks(
        CheckResult(name="metric", status=CheckStatus.PASS, detail="ok", before="5", after="0"),
        CheckResult(name="health", status=CheckStatus.PASS, detail="up"),
    )
    v = _verifier(itsm, checks, tmp_path)
    report = v.run(_ctx())
    assert report is not None
    assert report.verdict is CheckStatus.PASS
    # Proof work note written to the ticket, with before/after numbers.
    assert len(itsm.work_notes) == 1
    assert "verdict: PASS" in itsm.work_notes[0]
    assert "before=5, after=0" in itsm.work_notes[0]
    st = v.status()
    assert st["verified_total"] == 1 and st["passed_total"] == 1


# ─── fail path ───────────────────────────────────────────────────────────────


def test_fail_writes_persist_note_no_closure(tmp_path):
    itsm = FakeItsm()
    checks = _checks(
        CheckResult(name="metric", status=CheckStatus.FAIL, detail="still high", after="9"),
    )
    v = _verifier(itsm, checks, tmp_path)
    report = v.run(_ctx())
    assert report.verdict is CheckStatus.FAIL
    note = itsm.work_notes[0]
    assert "verdict: FAIL" in note
    assert "symptoms persist" in note.lower()
    assert v.status()["failed_total"] == 1


# ─── data-unavailable path ───────────────────────────────────────────────────


def test_skipped_checks_do_not_fail_and_are_flagged(tmp_path):
    itsm = FakeItsm()
    checks = _checks(
        CheckResult(
            name="metric", status=CheckStatus.SKIPPED, detail="no data (source unavailable)"
        ),
        CheckResult(name="logs (loki)", status=CheckStatus.SKIPPED, detail="no provider"),
    )
    v = _verifier(itsm, checks, tmp_path)
    report = v.run(_ctx())
    # No failures → PASS, but the note must flag what was NOT verified.
    assert report.verdict is CheckStatus.PASS
    assert len(report.skipped) == 2
    note = itsm.work_notes[0]
    assert "SKIPPED" in note
    assert "were NOT verified" in note


def test_default_checks_skip_when_metrics_unavailable(tmp_path):
    # No metric queries configured + a metrics_call that raises → all skipped.
    def boom(_q):
        raise RuntimeError("prometheus unreachable")

    v = Verifier(
        itsm_call=FakeItsm(),
        metrics_call=boom,
        windows=[0.0],
        sleep_fn=lambda _s: None,
        state_file=tmp_path / "v.json",
    )
    report = v.run(
        VerifyContext(incident_id="INC9", service="cart", alert_signature="up", metric_query="m")
    )
    assert report.verdict is CheckStatus.PASS
    assert all(c.status is CheckStatus.SKIPPED for c in report.checks)


# ─── idempotency ─────────────────────────────────────────────────────────────


def test_idempotent_second_run_is_noop(tmp_path):
    itsm = FakeItsm()
    checks = _checks(CheckResult(name="m", status=CheckStatus.PASS, detail="ok"))
    v = _verifier(itsm, checks, tmp_path)
    v.run(_ctx(incident="INC-DUP"))
    second = v.run(_ctx(incident="INC-DUP"))
    assert second is None  # already verified
    assert len(itsm.work_notes) == 1  # not written twice
    assert v.status()["verified_total"] == 1


def test_ledger_persists_across_instances(tmp_path):
    itsm = FakeItsm()
    checks = _checks(CheckResult(name="m", status=CheckStatus.PASS, detail="ok"))
    state = tmp_path / "verifier.json"
    Verifier(
        itsm_call=itsm, checks=checks, windows=[0.0], sleep_fn=lambda _s: None, state_file=state
    ).run(_ctx(incident="INC-P"))
    # Fresh instance loads the ledger → second run is skipped.
    v2 = Verifier(
        itsm_call=itsm, checks=checks, windows=[0.0], sleep_fn=lambda _s: None, state_file=state
    )
    assert v2.already_verified("INC-P") is True
    assert v2.run(_ctx(incident="INC-P")) is None


# ─── disabled ────────────────────────────────────────────────────────────────


def test_disabled_via_env(monkeypatch, tmp_path):
    monkeypatch.setenv("VERIFIER_ENABLED", "false")
    v = _verifier(
        FakeItsm(), _checks(CheckResult(name="m", status=CheckStatus.PASS, detail="ok")), tmp_path
    )
    assert v.run(_ctx()) is None
