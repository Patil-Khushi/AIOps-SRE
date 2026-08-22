"""RA-004's boundaries, stated as controls rather than promises (§26, §35–§37).

"The executor does not diagnose, does not declare recovery, does not execute anything
an LLM wrote, and cannot consume a Knowledge Synthesizer proposal directly." Docstrings
saying so are not enforcement — these are checked against the package's AST, the same
discipline ``tests/test_rca_chat_boundary.py`` and ``tests/test_rca_learning.py`` use,
and for the same reason: a substring scan over the source would flag the documentation
that explains the rule.

The scan covers every module under ``agents/runbook_executor/`` so a new file inherits
the constraints instead of quietly escaping them.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from agents.runbook_executor import ExecutorStatus, VerificationHandoff
from agents.runbook_executor.results import ExecutorResult

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "agents" / "runbook_executor"
MODULES = sorted(p for p in PACKAGE.glob("*.py") if p.name != "__main__.py")
SOURCES = {p.name: p.read_text(encoding="utf-8") for p in MODULES}


def _imported_modules(source: str) -> set[str]:
    tree = ast.parse(source)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _call_targets(source: str) -> tuple[set[str], set[str]]:
    """(bare names, dotted paths) appearing in a call position.

    Both are needed to tell ``eval(...)`` from ``re.compile(...)``: the first is a bare
    name that must never appear, the second is a perfectly ordinary dotted call. A scan
    that lumped them together would either miss ``eval`` or ban regex compilation.
    """
    tree = ast.parse(source)
    bare: set[str] = set()
    dotted: set[str] = set()

    def _path(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _path(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            bare.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            path = _path(node.func)
            if path:
                dotted.add(path)
            dotted.add(node.func.attr)
    return bare, dotted


def _called_names(source: str) -> set[str]:
    """Every name and attribute that appears in a call position (union view)."""
    bare, dotted = _call_targets(source)
    return bare | dotted


def _decorator_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        for dec in getattr(node, "decorator_list", []) or []:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


def test_the_package_has_modules_to_scan():
    """A vacuous scan would pass forever. Prove it is looking at something."""
    assert len(SOURCES) >= 10
    assert "agent.py" in SOURCES and "actions.py" in SOURCES


# ─── no LLM, no arbitrary execution (§11, §35) ───────────────────────────────


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_no_llm_anywhere_in_the_executor(module):
    """Selection, risk and validation are deterministic. There is no model in the loop,
    so there is nothing for a prompt injection to steer."""
    forbidden = {"aiops.llm", "anthropic", "openai", "ollama"}
    imported = _imported_modules(SOURCES[module])
    assert not {m for m in imported if any(m.startswith(f) for f in forbidden)}, module


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_no_shell_or_dynamic_execution(module):
    """No subprocess, no eval/exec, no dynamic import — the only way to affect the
    world is a registered capability."""
    imported = _imported_modules(SOURCES[module])
    assert not {
        m
        for m in imported
        if m.split(".")[0] in {"subprocess", "shlex", "pty", "popen2", "commands"}
    }, module
    bare, dotted = _call_targets(SOURCES[module])
    assert not (bare & {"eval", "exec", "compile", "__import__"}), module
    assert not (dotted & {"os.system", "os.popen", "os.execv", "subprocess.run"}), module


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_the_executor_never_registers_or_reroutes_a_tool(module):
    """§11: the action registry and the tool registry are read, never written. An agent
    that could register a provider could give itself a new capability."""
    called = _called_names(SOURCES[module])
    assert "select_provider" not in called, module
    assert "register" not in called, module
    assert "tool" not in _decorator_names(SOURCES[module]), module


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_the_executor_never_checks_the_gate_itself(module):
    """CLAUDE.md #3: HITL is enforced at the registry boundary. An agent that called
    the gate itself could also decide not to."""
    imported = _imported_modules(SOURCES[module])
    assert not {m for m in imported if m.startswith("aiops.policy")}, module
    called = _called_names(SOURCES[module])
    assert "get_gate" not in called and "enforce" not in called, module


# ─── no diagnosis (§35, §37) ─────────────────────────────────────────────────


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_the_executor_never_imports_rca(module):
    """RCA determines root cause. Routing to it is a ``next_action`` value, not a call."""
    imported = _imported_modules(SOURCES[module])
    assert not {m for m in imported if m.startswith("agents.rca_agent")}, module


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_the_executor_never_imports_the_resolution_verifier(module):
    """§26: recovery is the verifier's verdict. The executor produces a handoff payload
    and the API layer triggers the verifier — importing it here is how the two
    responsibilities would start to merge."""
    imported = _imported_modules(SOURCES[module])
    assert not {m for m in imported if m.startswith("agents.resolution_verifier")}, module


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_the_executor_never_imports_the_knowledge_synthesizer(module):
    """§36: a KS proposal reaches the executor only after a human approves it into an
    ACTIVE runbook version. A direct import would be the shortcut that bypasses review."""
    imported = _imported_modules(SOURCES[module])
    assert not {m for m in imported if m.startswith("agents.knowledge_synthesizer")}, module


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_the_executor_does_not_read_the_descriptive_runbook_library(module):
    """``aiops.runbooks`` is the prose library the Knowledge Synthesizer writes into,
    with no review gate the executor can see. Executable runbooks come from the
    version-controlled library only."""
    imported = _imported_modules(SOURCES[module])
    assert not {m for m in imported if m.startswith("aiops.runbooks")}, module


@pytest.mark.parametrize("module", sorted(SOURCES))
def test_the_executor_does_not_query_observability(module):
    """The executor is a control plane, not an evidence collector: it never re-reads
    metrics or logs. Recovery signals belong to the verifier; evidence to RCA. Whoever
    calls the executor supplies the incident facts."""
    for capability in (
        "observability.metrics.query",
        "observability.metrics.alerts",
        "observability.logs.query",
        "observability.traces.search",
    ):
        assert f'"{capability}"' not in SOURCES[module], f"{module} references {capability}"


# ─── the contract cannot claim resolution (§26) ──────────────────────────────


def test_no_executor_status_means_resolved():
    values = {s.value for s in ExecutorStatus}
    assert "EXECUTED" in values
    for forbidden in ("RESOLVED", "FIXED", "RECOVERED", "VERIFIED"):
        assert forbidden not in values


def test_the_result_has_no_field_a_recovery_claim_could_go_in():
    fields = set(ExecutorResult.model_fields)
    for forbidden in ("resolved", "recovered", "verified", "root_cause", "verification_verdict"):
        assert forbidden not in fields


def test_the_handoff_carries_no_verdict():
    """§29: the handoff says what was done. What it achieved is the verifier's answer."""
    fields = set(VerificationHandoff.model_fields)
    assert {"execution_id", "incident_id", "steps", "actions_executed"} <= fields
    for forbidden in ("verdict", "passed", "resolved", "recovered", "healthy"):
        assert forbidden not in fields


def test_verify_is_the_only_next_action_after_execution():
    """An executed plan always goes to verification — never straight to "done"."""
    from agents.runbook_executor.execution_state import ExecutionState, terminal_outcome

    status, next_action = terminal_outcome(ExecutionState.COMPLETED)
    assert status is ExecutorStatus.EXECUTED
    assert next_action.value == "VERIFY"


@pytest.mark.parametrize(
    "state",
    ["FAILED", "ROLLED_BACK", "ABORTED"],
)
def test_every_non_executed_terminal_state_routes_to_a_human_or_rca(state):
    from agents.runbook_executor.execution_state import ExecutionState, terminal_outcome

    _status, next_action = terminal_outcome(ExecutionState[state])
    assert next_action.value in ("RCA", "ESCALATE")


# ─── layering ────────────────────────────────────────────────────────────────


def test_the_platform_never_imports_the_executor():
    """``aiops/`` must not depend on an agent (the dependency arrow is demo → agents →
    aiops). ``tests/test_layering.py`` enforces this globally; this is the RA-004-specific
    statement of it, so a violation names this agent.

    Import-based, not substring-based, for the reason every other AST check in this repo
    gives: a mention in a comment is not a dependency. ``aiops/tools/mock_providers.py``
    legitimately explains *in prose* that RA-004 validates step parameters before
    dispatch, and a text scan flagged that explanation as a layering violation.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "aiops"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        imported = _imported_modules(path.read_text(encoding="utf-8"))
        if any("runbook_executor" in module for module in imported):
            offenders.append(path.relative_to(root.parent).as_posix())
    assert offenders == [], offenders
