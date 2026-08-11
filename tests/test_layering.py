"""Repo invariant: the platform layer does not depend on the agents above it.

The dependency arrow is ``demo/ → agents/ → aiops/``. Nothing enforced it until
now — ``tests/test_smoke.py`` guards vendor-SDK imports and FastAPI lifespan usage,
but the layering itself was convention only, which means the first import that
reversed it would have merged silently.

This matters most for ``aiops/context/``. The Context Engineering Layer is
consumed by four agents, and the obvious shortcut while writing it is to import an
agent's model — ``CorrelatedSignal`` to build a signal, RA-007's ``Evidence`` to
avoid defining an ``Observation``, ``knowledge_synthesizer``'s redaction rules
instead of writing new ones. Each is a small convenience that would make the
platform depend on one agent's vocabulary and stop every other agent from being
sellable on its own.

Uses AST rather than substring matching so a docstring that *mentions* an import
(this module's own does, several times) does not trip the check — the same
reasoning ``test_no_fastapi_on_event_in_demo_ui`` records.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKIP_DIR_PARTS = frozenset({"__pycache__", ".venv", ".claude", "build", "dist", "node_modules"})

AIOPS_TO_AGENTS_ALLOWLIST: frozenset[str] = frozenset(
    {
        # The orchestration seam is the one part of aiops/ that sits *above* the
        # agents by design: run_reactive_flow() chains RA-001 → 002 → 003 → 005+006,
        # so it must import them. Sanctioned by docs/CODEBASE_INTERNALS.md:10, which
        # also lists aiops.runtime as an import agents are allowed to make in return.
        # Recorded as an explicit exception rather than a blanket carve-out so a new
        # violation anywhere else in aiops/ still fails this test.
        "aiops/runtime/orchestrator.py",
    }
)


def _python_files(root: Path) -> list[Path]:
    return [
        path
        for path in root.rglob("*.py")
        if not _SKIP_DIR_PARTS.intersection(path.relative_to(REPO_ROOT).parts)
    ]


def _imported_roots(path: Path) -> set[str]:
    """Top-level package name of every import in ``path``.

    A relative import (``from . import x``) has ``node.level > 0`` and no top-level
    package to report, so it is skipped rather than misread as importing whatever
    ``node.module`` happens to say.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_context_layer_never_imports_agents_or_demo():
    """``aiops/context/`` is agent-agnostic, with no exceptions.

    Agent-specific projection belongs in ``agents/<name>/context_adapter.py``: an
    adapter has to reproduce its agent's own prompt vocabulary (RCA's
    ``"  (ABOVE the 2s threshold)"``, its ``pod {pod}: cpu=...`` format), and those
    strings are the agent's to change. Holding them in the platform would mean a
    prompt tweak needs a platform PR — worse coupling than the duplication this
    layer removes.
    """
    context_dir = REPO_ROOT / "aiops" / "context"
    if not context_dir.exists():  # pragma: no cover - package exists once Phase 0 lands
        return

    offenders: list[str] = []
    for path in _python_files(context_dir):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for root in sorted(_imported_roots(path) & {"agents", "demo"}):
            offenders.append(f"{rel}: imports {root!r}")

    assert not offenders, (
        "aiops/context/ must stay independent of every agent.\n"
        "Put agent-specific projection in agents/<name>/context_adapter.py instead.\n"
        + "\n".join(offenders)
    )


def test_aiops_never_imports_agents_outside_the_orchestration_seam():
    """The platform layer does not reach up into the agents.

    ``aiops/runtime/orchestrator.py`` is the sanctioned exception and is listed in
    ``AIOPS_TO_AGENTS_ALLOWLIST``. Adding a new entry there should be a deliberate
    architectural decision made in review, not a way to make this test pass.
    """
    offenders: list[str] = []
    for path in _python_files(REPO_ROOT / "aiops"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in AIOPS_TO_AGENTS_ALLOWLIST:
            continue
        if "agents" in _imported_roots(path):
            offenders.append(rel)

    assert not offenders, (
        "aiops/ must not import agents/ (dependency arrow is demo/ -> agents/ -> aiops/).\n"
        "If this is genuinely a new orchestration seam, add it to "
        "AIOPS_TO_AGENTS_ALLOWLIST with a comment saying why.\n" + "\n".join(offenders)
    )


def test_aiops_never_imports_demo():
    """The platform never depends on the demo harness — no allowlist at all.

    ``demo/`` registers providers *into* ``aiops`` (``demo/providers.py``,
    ``demo/ui/fault_clear.py``); the arrow only runs that way. An import in the
    other direction would make the platform unusable without the demo SUT, which
    defeats the point of every agent being individually sellable.
    """
    offenders: list[str] = []
    for path in _python_files(REPO_ROOT / "aiops"):
        if "demo" in _imported_roots(path):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert not offenders, "aiops/ must not import demo/:\n" + "\n".join(offenders)
