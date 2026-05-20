"""Tests for the Stage 6 ownership block.

Pins the explicit non-empty-string contract: CMDB and on-call fields must
be strings with content before they override defaults / populate the verdict.
The prior ``... or default`` truthiness pattern silently treated 0, False,
empty lists, etc. as 'no value' — which happened to work for the mock but
is the wrong contract once real providers (ServiceNow, PagerDuty) land.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest


@pytest.fixture
def clean_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    db_path = tmp_path / "test_state.db"
    monkeypatch.setenv("AIOPS_STATE_DB_URL", f"sqlite:///{db_path.as_posix()}")

    from aiops.state import init_db, reset_engine_for_tests

    reset_engine_for_tests()
    init_db()

    from agents.alert_triage.agent import reset_state

    reset_state()

    yield

    reset_engine_for_tests()


def _alert_input(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "alert_id": "ALT-OWN",
        "service": "payment",
        "metric": "CPU Usage",
        "value": 90.0,
        "threshold": 80.0,
        "timestamp": datetime.now(UTC).isoformat(),
        "source": "Prometheus",
        "labels": {},
        "annotations": {},
    }
    base.update(overrides)
    return base


def _stub_cmdb(monkeypatch, data: Any) -> None:
    """Swap the active CMDB provider for one returning ``data``. ``data`` is
    placed inside ``ToolResult(ok=True, data=data)``. Pass ``None`` for the
    'no match' case."""
    from aiops.tools import ToolResult, get_registry
    from aiops.tools.registry import Tool

    registry = get_registry()

    def _fake_cmdb(service: str) -> ToolResult:
        return ToolResult(ok=True, data=data)

    tool_name = f"test.fake_cmdb_{id(_fake_cmdb)}"
    registry._tools[tool_name] = Tool(  # type: ignore[attr-defined]
        name=tool_name,
        description="test stub",
        fn=_fake_cmdb,
        capability="itsm.cmdb.lookup",
        provider="test",
    )
    prior = registry._active.get("itsm.cmdb.lookup")  # type: ignore[attr-defined]
    registry._active["itsm.cmdb.lookup"] = tool_name  # type: ignore[attr-defined]
    monkeypatch.setattr(
        registry, "_active", {**registry._active, "itsm.cmdb.lookup": tool_name},  # type: ignore[attr-defined]
        raising=False,
    )
    # The above monkeypatch.setattr makes the restoration automatic at teardown.
    _ = prior  # snapshot recorded


def _stub_oncall(monkeypatch, data: Any) -> None:
    from aiops.tools import ToolResult, get_registry
    from aiops.tools.registry import Tool

    registry = get_registry()

    def _fake_oncall(team: str) -> ToolResult:
        return ToolResult(ok=True, data=data)

    tool_name = f"test.fake_oncall_{id(_fake_oncall)}"
    registry._tools[tool_name] = Tool(  # type: ignore[attr-defined]
        name=tool_name,
        description="test stub",
        fn=_fake_oncall,
        capability="oncall.schedule.lookup",
        provider="test",
    )
    monkeypatch.setattr(
        registry, "_active", {**registry._active, "oncall.schedule.lookup": tool_name},  # type: ignore[attr-defined]
        raising=False,
    )


# ─── happy path baseline (sanity that the test wiring works) ───────────────


def test_normal_cmdb_payload_populates_verdict(clean_state, monkeypatch):
    _stub_cmdb(monkeypatch, {
        "service": "payment",
        "team": "Payments Team",
        "runbook": "https://runbooks.example.com/payment-cpu",
    })
    _stub_oncall(monkeypatch, {
        "team": "Payments Team",
        "engineer_email": "oncall@payments.example.com",
    })

    from agents.alert_triage import run

    v = run(_alert_input())
    assert v["assigned_team"] == "Payments Team"
    assert v["assigned_engineer"] == "oncall@payments.example.com"
    assert v["recommended_runbook"] == "https://runbooks.example.com/payment-cpu"


# ─── non-string / falsy edge cases — the actual contract test ──────────────


@pytest.mark.parametrize(
    "weird_team",
    [
        0,        # ServiceNow could return numeric IDs in a misconfigured field
        False,    # accidental bool serialization
        [],       # empty list (e.g. multi-team field misrendered)
        {},
    ],
    ids=["int-zero", "bool-false", "empty-list", "empty-dict"],
)
def test_non_string_cmdb_team_falls_back_to_default(
    clean_state, monkeypatch, weird_team: Any
) -> None:
    """A non-string in the team field must NOT silently land in the verdict.
    The agent falls back to the platform default and the verdict still
    parses cleanly (TriageVerdict.assigned_team requires str)."""
    _stub_cmdb(monkeypatch, {"team": weird_team, "runbook": None})

    from agents.alert_triage import run

    v = run(_alert_input())
    assert v["assigned_team"] == "Platform On-Call", (
        f"weird team {weird_team!r} leaked into verdict"
    )


def test_empty_string_team_does_not_overwrite_default(clean_state, monkeypatch):
    """The prior ``or`` pattern also defaulted on empty string; preserve
    that behavior — but via an explicit check, so weird non-string values
    are no longer silently swallowed alongside it."""
    _stub_cmdb(monkeypatch, {"team": "   ", "runbook": ""})

    from agents.alert_triage import run

    v = run(_alert_input())
    assert v["assigned_team"] == "Platform On-Call"
    assert v["recommended_runbook"] is None


def test_team_with_surrounding_whitespace_is_stripped(clean_state, monkeypatch):
    """CMDB may yield data with stray whitespace. Strip so the verdict
    value is clean and downstream comparisons don't fail on padding."""
    _stub_cmdb(monkeypatch, {"team": "  Payments Team  ", "runbook": "  https://r.example  "})

    from agents.alert_triage import run

    v = run(_alert_input())
    assert v["assigned_team"] == "Payments Team"
    assert v["recommended_runbook"] == "https://r.example"


def test_empty_engineer_email_results_in_none(clean_state, monkeypatch):
    """Empty engineer_email must NOT land in the verdict as ``""``.
    TriageVerdict's ``assigned_engineer`` is ``str | None``; "" passes
    Pydantic but is semantically wrong — would render in UIs as a missing
    name with a colon next to it."""
    _stub_cmdb(monkeypatch, {"team": "Payments Team", "runbook": None})
    _stub_oncall(monkeypatch, {"team": "Payments Team", "engineer_email": ""})

    from agents.alert_triage import run

    v = run(_alert_input())
    assert v["assigned_engineer"] is None
