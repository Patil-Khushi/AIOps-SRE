"""Hand-rolled eval harness.

Each agent ships a ``evals/golden.json`` next to its code with a list of cases.
Run all agents' evals with::

    uv run python -m evals.harness

Or one agent::

    uv run python -m evals.harness --agent ra-001-alert-triage

CI mode prints a JSON summary and exits non-zero if pass rate is below threshold::

    uv run python -m evals.harness --ci --min-pass-rate 0.85
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from aiops._dotenv import load_dotenv
from aiops.state import init_db

from .scoring import score_case

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


@dataclass
class Case:
    id: str
    description: str
    input: dict[str, Any]
    expected: dict[str, Any]
    tags: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    passed: bool
    score: float
    details: dict[str, Any]
    duration_ms: int


@dataclass
class AgentRun:
    agent: str
    results: list[CaseResult]

    @property
    def pass_rate(self) -> float:
        if not self.results:
            return 1.0
        return sum(1 for r in self.results if r.passed) / len(self.results)


def _load_cases(path: Path) -> list[Case]:
    """Accept a flat list or a ``{"cases": [...]}`` wrapper (the wrapped form
    lets goldens carry top-level metadata like agent/version/description).
    Per-case keys not on ``Case`` are dropped so goldens can hold extra
    metadata (e.g. a ``scenario`` cross-ref) without coupling the schema."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("cases", [])
    case_fields = {f.name for f in fields(Case)}
    return [Case(**{k: v for k, v in c.items() if k in case_fields}) for c in raw]


def _resolve_runner(agent_dir: Path) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Import ``<agent_module>.run`` for the agent.

    Convention: ``agents/<slug>/agent.py`` exposes ``run(input: dict) -> dict``.
    """
    rel = agent_dir.relative_to(REPO_ROOT).as_posix().replace("/", ".")
    module_name = f"{rel}.agent"
    mod = importlib.import_module(module_name)
    if not hasattr(mod, "run"):
        raise AttributeError(f"{module_name} does not define a ``run(input)`` function")
    return mod.run  # type: ignore[no-any-return]


def _resolve_reset_state(agent_dir: Path) -> Callable[[], None] | None:
    """Optional ``reset_state()`` hook so agents with persistent state can
    expose a clean-slate between cases. Returns None when the agent doesn't
    define one (stateless agents need no hook)."""
    rel = agent_dir.relative_to(REPO_ROOT).as_posix().replace("/", ".")
    mod = importlib.import_module(f"{rel}.agent")
    fn = getattr(mod, "reset_state", None)
    return fn if callable(fn) else None


def run_agent(agent_dir: Path) -> AgentRun:
    cases_path = agent_dir / "evals" / "golden.json"
    if not cases_path.exists():
        return AgentRun(agent=agent_dir.name, results=[])
    cases = _load_cases(cases_path)
    runner = _resolve_runner(agent_dir)
    reset_state = _resolve_reset_state(agent_dir)
    results: list[CaseResult] = []
    for c in cases:
        if reset_state is not None:
            reset_state()
        t0 = time.perf_counter()
        try:
            actual = runner(c.input)
            scored = score_case(actual=actual, expected=c.expected)
        except Exception as exc:
            scored = {"passed": False, "score": 0.0, "details": {"error": str(exc)}}
        duration_ms = int((time.perf_counter() - t0) * 1000)
        results.append(
            CaseResult(
                case_id=c.id,
                passed=bool(scored["passed"]),
                score=float(scored["score"]),
                details=scored.get("details", {}),
                duration_ms=duration_ms,
            )
        )
    return AgentRun(agent=agent_dir.name, results=results)


def discover_agents() -> list[Path]:
    agents_root = REPO_ROOT / "agents"
    if not agents_root.exists():
        return []
    return sorted(d for d in agents_root.iterdir() if d.is_dir() and (d / "agent.py").exists())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run hand-rolled evals across agents")
    p.add_argument("--agent", help="Run a single agent (directory name under agents/)")
    p.add_argument(
        "--ci", action="store_true", help="Emit JSON summary; exit non-zero on regression"
    )
    p.add_argument("--min-pass-rate", type=float, default=0.85, help="CI threshold (0..1)")
    args = p.parse_args(argv)

    agent_dirs = discover_agents()
    if args.agent:
        agent_dirs = [d for d in agent_dirs if d.name == args.agent]
        if not agent_dirs:
            print(f"agent {args.agent!r} not found", file=sys.stderr)
            return 2

    if not agent_dirs:
        # Phase 0: no agents yet. Treat as a clean run so CI is green.
        summary = {"agents": [], "overall_pass_rate": 1.0, "phase0": True}
        print(json.dumps(summary, indent=2))
        return 0

    # Agents like alert_triage have persisted dedup state in SQLite — their
    # reset_state() hook deletes rows, which requires the schema to exist.
    # CI starts from a fresh checkout with no data/state.db; locally the file
    # may already exist from a prior UI run. Idempotent either way.
    init_db()

    runs = [run_agent(d) for d in agent_dirs]
    overall = sum(r.pass_rate for r in runs) / len(runs)
    summary = {
        "agents": [
            {"agent": r.agent, "pass_rate": r.pass_rate, "results": [asdict(x) for x in r.results]}
            for r in runs
        ],
        "overall_pass_rate": overall,
    }
    print(json.dumps(summary, indent=2))
    if args.ci and overall < args.min_pass_rate:
        print(
            f"FAIL: overall pass rate {overall:.2%} below threshold {args.min_pass_rate:.2%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
