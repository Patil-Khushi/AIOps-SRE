"""Phase-0 smoke tests.

Exercise the three platform seams end-to-end with the stub LLM and mock tool
providers. These tests are what CI runs to confirm the wiring works before any
agent exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- aiops.llm ----------------------------------------------------------


@pytest.fixture(autouse=True)
def _stub_provider(monkeypatch):
    monkeypatch.setenv("AIOPS_LLM_PROVIDER", "stub")
    yield


def test_llm_complete_through_gateway():
    from aiops.llm import Message, complete

    resp = complete(
        [Message("system", "you are a tester"), Message("user", "ping")],
        max_tokens=64,
    )
    assert resp.provider == "stub"
    assert "ping" in resp.text
    assert resp.input_tokens > 0


def test_llm_caps_clamp_max_tokens(monkeypatch):
    monkeypatch.setenv("AIOPS_LLM_MAX_TOKENS_PER_CALL", "32")
    from aiops.llm import Message, complete

    resp = complete([Message("user", "hi")], max_tokens=99999)
    assert resp.provider == "stub"


# --- aiops.tools --------------------------------------------------------


def test_tool_registry_dispatches_by_capability():
    # importing the mock_providers module triggers @tool registration.
    from aiops.tools import (
        get_registry,
        mock_providers,  # noqa: F401
    )

    reg = get_registry()
    # Force the mock provider for this assertion. When .env has
    # AIOPS_USE_MOCK_ITSM=false the servicenow provider also registers and
    # whichever module imported first wins via the registry's setdefault.
    # The README documents `select_provider` as the swap pattern.
    reg.select_provider("itsm.incident.create", "mock.itsm.incident.create")
    result = reg.call(
        "itsm.incident.create",
        short_description="payments DB pool exhausted",
        urgency=1,
    )
    assert result.ok
    assert result.data["short_description"] == "payments DB pool exhausted"
    assert result.metadata["provider"] == "mock"


def test_tool_registry_unknown_capability():
    from aiops.tools import get_registry

    with pytest.raises(KeyError):
        get_registry().call("does.not.exist")


# --- aiops.policy -------------------------------------------------------


def test_hitl_gate_allows_none_level():
    from aiops.policy import AutonomyLevel, get_gate

    g = get_gate()
    d = g.check("notify.send")
    assert d.allowed
    assert d.level is AutonomyLevel.NONE


def test_hitl_gate_blocks_required_without_approver():
    from aiops.policy import AutonomyLevel, GateError, get_gate

    g = get_gate()
    d = g.check("rca.fix_step.execute")
    assert d.level is AutonomyLevel.REQUIRED
    assert not d.allowed
    with pytest.raises(GateError):
        g.enforce("rca.fix_step.execute")


def test_hitl_gate_optional_respects_tenant_flag():
    from aiops.policy import AutonomyLevel, get_gate

    g = get_gate()
    d = g.check("itsm.incident.create", {"tenant_requires_hitl": False})
    assert d.allowed
    assert d.level is AutonomyLevel.OPTIONAL
    d2 = g.check("itsm.incident.create", {"tenant_requires_hitl": True})
    assert not d2.allowed


# --- truth files match scenarios ------------------------------------------


def test_every_scenario_has_a_truth_file():
    """DEMO-12 (#64): one folder, one rule — every scenario in
    ``demo/scenarios/`` ships with a paired truth file. CLAUDE.md non-
    negotiable #8: "truth files for every demo scenario"."""
    scenarios_dir = REPO_ROOT / "demo" / "scenarios"
    truth_dir = REPO_ROOT / "demo" / "truth_files"
    scenario_ids = {
        yaml.safe_load(p.read_text(encoding="utf-8"))["id"] for p in scenarios_dir.glob("*.yaml")
    }
    truth_ids = {p.stem for p in truth_dir.glob("*.yaml") if p.stem != "template"}
    missing = scenario_ids - truth_ids
    assert not missing, f"scenarios without truth files: {sorted(missing)}"


def test_truth_template_is_valid_yaml():
    template = REPO_ROOT / "demo" / "truth_files" / "template.yaml"
    data = yaml.safe_load(template.read_text(encoding="utf-8"))
    assert "scenario_id" in data
    assert "expected_rca" in data
    assert "expected_fix_steps" in data


# --- eval harness handles empty agents/ ------------------------------------


def test_eval_harness_phase0_passes_with_no_agents(capsys, monkeypatch):
    from evals import harness

    monkeypatch.setattr(harness, "discover_agents", lambda: [])
    rc = harness.main(["--ci", "--min-pass-rate", "0.85"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"phase0": true' in out


def test_load_cases_accepts_both_shapes_and_drops_unknown_keys(tmp_path):
    from evals.harness import _load_cases

    flat = tmp_path / "flat.json"
    flat.write_text(
        json.dumps([{"id": "a", "description": "d", "input": {}, "expected": {}}]), encoding="utf-8"
    )
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps(
            {
                "agent": "X",
                "version": "v1",
                "cases": [
                    {
                        "id": "b",
                        "description": "d",
                        "scenario": "extra-key-dropped",
                        "input": {},
                        "expected": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert [c.id for c in _load_cases(flat)] == ["a"]
    assert [c.id for c in _load_cases(wrapped)] == ["b"]


# --- repo invariant: no direct vendor SDK imports outside aiops/llm ------


def test_no_direct_llm_sdk_imports_outside_aiops_llm():
    """Solution Design §2 / POC guide §9.6 — vendor-neutrality wrapper.

    Agent code must never import vendor SDKs directly. Phase 0 has no agents,
    so this is a guard for when they land.
    """
    forbidden = ("import anthropic", "import openai", "from anthropic", "from openai")
    offenders: list[str] = []
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel.startswith(("aiops/llm/", "tests/", ".tmp_", "build/", "dist/")):
            continue
        if rel.startswith((".venv/", ".claude/")):
            continue
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                offenders.append(f"{rel}: {needle}")
    assert not offenders, "Direct LLM SDK imports outside aiops/llm:\n" + "\n".join(offenders)
