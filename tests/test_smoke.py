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


def test_rca_fix_step_rejects_requires_hitl_false():
    """PRS-008 catalog invariant: every RCA fix step is Required-HITL. The
    ``Literal[True]`` field type defends the invariant at the schema layer so
    a future caller that bypasses ``_coerce_verdict`` and feeds raw LLM JSON
    into ``RankedFixStep`` cannot smuggle ``requires_hitl=false`` through."""
    from pydantic import ValidationError

    from agents.rca_agent.models import BlastRadius, RankedFixStep

    # Default path still works.
    step = RankedFixStep(description="x", blast_radius=BlastRadius.LOW, rollback="y")
    assert step.requires_hitl is True

    # Explicit True is allowed.
    step = RankedFixStep(
        description="x", blast_radius=BlastRadius.LOW, rollback="y", requires_hitl=True
    )
    assert step.requires_hitl is True

    # Explicit False is rejected at the schema layer.
    with pytest.raises(ValidationError):
        RankedFixStep(
            description="x", blast_radius=BlastRadius.LOW, rollback="y", requires_hitl=False
        )


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


def test_every_ecommerce_scenario_has_a_truth_file():
    """CLAUDE.md non-negotiable #8: no scenario without ground truth.

    Replaces test_truth_template_is_valid_yaml. demo/truth_files/template.yaml
    belonged to the OTel Demo suite and went with it; the ecommerce truth files
    are JSON and carry no template. The invariant worth enforcing was never the
    template's existence — it was that every scenario has ground truth.
    """
    scen_dir = REPO_ROOT / "demo" / "ecommerce" / "scenarios"
    truth_dir = REPO_ROOT / "demo" / "ecommerce" / "truth_files"
    scenarios = {p.stem for p in scen_dir.glob("*.yaml")}
    truths = {p.stem for p in truth_dir.glob("*.json")}
    assert scenarios, "no ecommerce scenarios found"
    missing = sorted(scenarios - truths)
    assert not missing, f"scenarios without a truth file: {missing}"


# --- eval harness handles empty agents/ ------------------------------------


def test_eval_harness_phase0_passes_with_no_agents(capsys, monkeypatch):
    """The harness emits a ``phase0: true`` shortcut when there's nothing to
    score, so CI is green on a fresh checkout. Post-EVAL-1 (#75) truth files
    are also discovered, so the test has to stub both sources to exercise the
    real "nothing to run" branch — otherwise this asserts the harness's
    error-path output instead of its empty-run output.
    """
    from evals import harness

    # Stub both discovery functions: the harness now emits phase0=true only
    # when neither agents nor truth files exist. EVAL-1 (#75) added the
    # truth-file pass; DEMO-12 (#64) backfilled 15 real truth files into
    # demo/truth_files/, so without this second stub the harness sees them
    # and skips the phase0 short-circuit.
    monkeypatch.setattr(harness, "discover_agents", lambda: [])
    monkeypatch.setattr(harness, "discover_truth_files", lambda: [])
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


def test_no_fastapi_on_event_in_demo_ui():
    """DEMO-15 (#67) — ratchet the lifespan migration.

    ``@app.on_event`` has been deprecated since FastAPI 0.104 and was
    removed from ``demo/ui/`` in favour of a single ``lifespan`` context
    manager. PR #135 (auto-triage loop) silently re-introduced it; this
    test exists so the next merge that does so fails CI instead.

    Uses AST so the lifespan docstring (which mentions @app.on_event
    historically) doesn't trip the check.
    """
    import ast

    offenders: list[str] = []
    for path in (REPO_ROOT / "demo" / "ui").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for deco in node.decorator_list:
                target = deco.func if isinstance(deco, ast.Call) else deco
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "on_event"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "app"
                ):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}:{node.lineno} @app.on_event on {node.name}")
    assert not offenders, (
        "FastAPI @app.on_event is deprecated — use the lifespan context manager:\n"
        + "\n".join(offenders)
    )
