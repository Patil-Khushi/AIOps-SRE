"""RCA evaluation matrix — the accuracy tier.

Two tiers exist and they measure different things. ``evals/harness.py`` runs the
agent goldens and truth-file ``exercises`` blocks in CI with no cluster and no LLM,
which can only assert *contract* properties. This module is the other tier: it hands
RCA simulated telemetry for each of the 12 ecommerce scenarios and scores whether it
actually finds the cause.

Run it::

    uv run python -m evals.rca_eval                          # baseline
    uv run python -m evals.rca_eval --mode no-evidence       # abstention contract
    uv run python -m evals.rca_eval --mode cold-start        # memory off (control)
    uv run python -m evals.rca_eval --mode learning          # leave-one-out memory
    uv run python -m evals.rca_eval --mode poisoned-memory   # memory safety
    uv run python -m evals.rca_eval --mode ablation          # all three, with deltas

Deliberately **not** wired into ``evals.harness`` or CI: it needs a real LLM, so in
CI it would score the fallback path and call the result accuracy. It is a
measurement instrument to be run and read by a human, and its output states its own
provider and limitations so a number cannot be quoted out of context.

The three memory arms, and which one to trust
---------------------------------------------
``cold-start`` disables memory entirely — the control. ``learning`` seeds each scenario
with verified outcomes from the *other* eleven, so a prior can never be the answer to the
incident being scored. ``poisoned-memory`` seeds a deliberately wrong precedent for each
scenario's own symptoms.

Read the **poisoned** arm first. The learning delta is a weak signal by construction: the
seeded memories are RCA's own predictions from a first pass, marked verified without
consulting the truth files, so they carry the agent's mistakes as well as its successes.
The poisoned arm needs no truth data at all and tests the property that actually matters —
current evidence beats a confident, verified, wrong precedent. A drop in accuracy there is
a defect in decay or reliability weighting, not a tuning opportunity.

Both memory arms run every scenario twice (harvest, then score) and write to a throwaway
SQLite file, never to ``data/state.db`` — an instrument must not mutate what it measures.

Blindness
---------
Inputs come from ``evals.rca_truth.rca_input_from_truth`` (which asserts blindness on
its own output) and ``evals.rca_synthetic.build_synthetic_context`` (observable
symptoms only). The grading key is read separately and never joins the input. No
``scenario_id`` is passed, so the deterministic fallback cannot recognise the
scenario under test.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiops._dotenv import load_dotenv

from .rca_metrics import MatrixReport, ScenarioScore, score_scenario
from .rca_synthetic import build_synthetic_context
from .rca_truth import (
    discover_ecommerce_truth_files,
    expected_from_truth,
    load_truth,
    rca_input_from_truth,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

MODES = (
    "baseline",
    "no-evidence",
    "cold-start",
    "learning",
    "poisoned-memory",
    "ablation",
)

_COLD_START_NOTE = (
    "cold start: AIOPS_RCA_MEMORY_PROVIDERS is empty, so no prior can be built. This is "
    "the control arm — identical to 'baseline' by construction, and run separately so the "
    "learning delta is measured against a run of the same code rather than a remembered one."
)

_LEARNING_NOTE = (
    "leave-one-out learning: for each scenario, memory is seeded with verified outcomes "
    "from the OTHER scenarios only. The held-out scenario's own outcome is excluded, so a "
    "prior can never be the answer to the incident being scored."
)

_LEARNING_SEED_NOTE = (
    "seeded outcomes are RCA's OWN predictions from a first pass, marked verified without "
    "consulting the truth files — the simulation stands in for a resolution verifier. So "
    "memory contains the agent's mistakes as well as its successes, which is what makes "
    "wrong_memory_influence_rate measurable. It also means these are not ground-truth "
    "memories and the learning delta must not be read as an accuracy claim."
)

_POISON_NOTE = (
    "poisoned memory: each scenario is seeded with a deliberately WRONG verified outcome "
    "carrying its own symptoms. This is the safety arm — it needs no truth data at all, "
    "and the property under test is that current evidence still wins. Any non-zero "
    "wrong_memory_influence_rate here is a defect in decay or reliability weighting."
)


@contextlib.contextmanager
def _context_layer(mode: str):
    """Temporarily select a context-layer mode for the duration of a run.

    The evaluation needs ``on`` so the agent consumes the synthetic Context Pack
    rather than reaching for a live Prometheus that is not there. Set through the
    environment because that is the documented control surface and it is read
    per-call (``aiops/context/config.py``), and restored afterwards so running the
    eval in-process cannot leave a changed global behind.
    """
    previous = os.environ.get("AIOPS_CONTEXT_LAYER")
    os.environ["AIOPS_CONTEXT_LAYER"] = mode
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AIOPS_CONTEXT_LAYER", None)
        else:
            os.environ["AIOPS_CONTEXT_LAYER"] = previous


@contextlib.contextmanager
def _memory_mode(enabled: bool):
    """Turn RCA's historical memory on or off for the duration of a run.

    Through the environment, because that is the documented control surface and
    ``memory.memory_providers`` reads it per call. An explicit empty string is honoured as
    a deliberate cold start rather than falling back to the default provider.
    """
    previous = os.environ.get("AIOPS_RCA_MEMORY_PROVIDERS")
    os.environ["AIOPS_RCA_MEMORY_PROVIDERS"] = "rca_outcomes" if enabled else ""
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AIOPS_RCA_MEMORY_PROVIDERS", None)
        else:
            os.environ["AIOPS_RCA_MEMORY_PROVIDERS"] = previous


@contextlib.contextmanager
def _isolated_memory_store(path: Path):
    """Point ``aiops.state`` at a throwaway SQLite file for this run.

    The evaluation *writes* outcomes in the learning and poisoned arms. Doing that against
    the developer's real ``data/state.db`` would leave synthetic memories behind to
    influence later real investigations — a measurement instrument must not mutate the
    thing it measures.
    """
    from aiops.state import init_db, reset_engine_for_tests

    previous = os.environ.get("AIOPS_STATE_DB_URL")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["AIOPS_STATE_DB_URL"] = f"sqlite:///{path.as_posix()}"
    reset_engine_for_tests()
    init_db()
    try:
        yield
    finally:
        reset_engine_for_tests()
        if previous is None:
            os.environ.pop("AIOPS_STATE_DB_URL", None)
        else:
            os.environ["AIOPS_STATE_DB_URL"] = previous
        reset_engine_for_tests()


def _seed_outcome_from_verdict(
    truth: dict[str, Any], verdict: dict[str, Any], *, wrong: bool = False
) -> None:
    """Record one scenario's RCA result as a verified outcome.

    Marked verified **without consulting the truth file**: the simulation stands in for a
    resolution verifier, which in a deployment confirms recovery by re-observing telemetry
    rather than by reading an answer key. The consequence is deliberate — memory ends up
    holding the agent's wrong answers too, which is exactly what makes
    ``wrong_memory_influence_rate`` a real measurement instead of a hopeful zero.

    ``wrong=True`` inverts the recorded cause and hypothesis to seed the poisoned arm.
    """
    from agents.rca_agent.investigation.memory import record_outcome
    from agents.rca_agent.investigation.models import RCAOutcome, RootCauseStatus

    investigation = verdict.get("investigation") or {}
    matrices = investigation.get("matrices") or []
    selected_id = investigation.get("selected_hypothesis_id")

    # The failure *class*, not the hypothesis id: ids are digest(incident_id, rule_id) and
    # therefore unique per incident, so a memory keyed on one can never match a later
    # incident. Resolved off the matrices because only they carry the category.
    def _class_of(entry: dict[str, Any]) -> str:
        return str((entry.get("hypothesis") or {}).get("category") or "")

    selected_class = next(
        (
            _class_of(m)
            for m in matrices
            if (m.get("hypothesis") or {}).get("hypothesis_id") == selected_id
        ),
        _class_of(matrices[0]) if matrices else "",
    )
    if wrong:
        # A rival class from the agent's own catalog, so the poison is plausible rather
        # than nonsense — a prior naming an impossible class would attach to no hypothesis
        # and would test nothing.
        rivals = [_class_of(m) for m in matrices if _class_of(m) != selected_class]
        selected_class = next((r for r in rivals if r), "resource_exhaustion_memory")
        selected_id = None

    signatures = _observable_signatures(truth)
    if not signatures or not selected_class:
        # No class means nothing a later recall could attach the prior to, so the row
        # would be dead weight in the store.
        return

    record_outcome(
        RCAOutcome(
            incident_id=f"SEED-{truth.get('id') or 'unknown'}{'-poison' if wrong else ''}",
            affected_service=str(truth.get("service") or "unknown"),
            recorded_at=datetime.now(UTC),
            predicted_root_cause=(
                f"seeded precedent: {selected_class}"
                if wrong
                else str(verdict.get("root_cause") or "")[:300]
            ),
            predicted_status=RootCauseStatus.PROBABLE,
            confidence=float(verdict.get("confidence_score") or 0.5),
            selected_hypothesis_id=selected_id,
            selected_hypothesis_class=selected_class,
            verification_result="resolved",
            extra={"signatures": signatures},
        )
    )


def _observable_signatures(truth: dict[str, Any]) -> list[str]:
    """Symptom identifiers for a seeded memory, from the truth file's *observable* signals.

    Alert name and metric/container/log signal names only. ``root_cause``,
    ``root_cause_keywords``, ``fault_category`` and ``remediation`` are never read — a
    seeded memory that carried the cause string would make the learning arm an answer-key
    lookup, which is the failure this whole split exists to prevent.
    """
    signatures: list[str] = []
    alertname = ((truth.get("expected_alert_payload") or {}).get("labels") or {}).get("alertname")
    if alertname:
        signatures.append(str(alertname))
    signals = truth.get("expected_signals") or {}
    for group in ("metrics", "container", "logs"):
        for entry in signals.get(group) or []:
            if isinstance(entry, dict) and entry.get("name"):
                signatures.append(str(entry["name"]))
    return signatures


def _run_one(
    truth: dict[str, Any], *, with_evidence: bool
) -> tuple[ScenarioScore, float, list[str]]:
    """Score one scenario. Returns ``(score, seconds, decision_trace)``."""
    from agents.rca_agent.agent import analyze

    payload = rca_input_from_truth(truth)
    expected = expected_from_truth(truth)

    context: dict[str, Any] | None = None
    coverage = 0.0
    unrepresentable: tuple[str, ...] = ()
    if with_evidence:
        pack, synthetic = build_synthetic_context(truth)
        context = pack.model_dump(mode="json")
        coverage = synthetic.coverage
        unrepresentable = tuple(synthetic.unrepresentable)

    started = time.perf_counter()
    verdict = analyze(payload["triage_verdict"], context=context)
    elapsed = time.perf_counter() - started

    verdict_dict = verdict.model_dump(mode="json")
    score = score_scenario(
        expected=expected,
        verdict=verdict_dict,
        evidence_coverage=coverage,
        unrepresentable=unrepresentable,
    )
    trace = list((verdict_dict.get("audit_metadata") or {}).get("decision_trace") or [])
    return score, elapsed, trace


def _verdict_for_seeding(truth: dict[str, Any]) -> dict[str, Any] | None:
    """One cold-start pass over a scenario, to harvest an outcome worth remembering.

    Run with memory disabled so the harvested prediction is not itself shaped by priors —
    otherwise round two would remember conclusions that round one reached *because of*
    memory, and the learning arm would be measuring its own echo.
    """
    from agents.rca_agent.agent import analyze

    payload = rca_input_from_truth(truth)
    pack, _ = build_synthetic_context(truth)
    with _memory_mode(False):
        verdict = analyze(payload["triage_verdict"], context=pack.model_dump(mode="json"))
    return verdict.model_dump(mode="json")


@contextlib.contextmanager
def _scratch_store_path():
    """A temp directory for the run's throwaway memory store."""
    with tempfile.TemporaryDirectory(prefix="rca-eval-memory-") as tmp:
        yield Path(tmp) / "memory.db"


def _reseed_store(
    truths: list[dict[str, Any]],
    harvested: dict[str, dict[str, Any]],
    *,
    held_out: str,
    poison: bool,
) -> None:
    """Rebuild memory for one leave-one-out round.

    Cleared and rewritten per scenario rather than filtered at query time. Exclusion at
    read time would depend on the recall call passing the right ``exclude_incident_ids``,
    and a leave-one-out design whose isolation rests on a caller remembering an argument is
    one bad refactor from silently leaking the held-out scenario's own answer.
    """
    from aiops.state.repository import delete_all_rca_outcomes

    delete_all_rca_outcomes()
    for truth in truths:
        scenario_id = str(truth.get("id"))
        if poison:
            # Every scenario gets a wrong precedent for its *own* symptoms — including the
            # one under test, since that is the whole point of the arm.
            verdict = harvested.get(scenario_id)
            if verdict:
                _seed_outcome_from_verdict(truth, verdict, wrong=True)
            continue
        if scenario_id == held_out:
            continue
        verdict = harvested.get(scenario_id)
        if verdict:
            _seed_outcome_from_verdict(truth, verdict)


def run_matrix(mode: str = "baseline", *, verbose: bool = False) -> MatrixReport:
    """Score every ecommerce scenario in one mode."""
    from agents.rca_agent.agent import _rca_model, _rca_provider

    with_evidence = mode != "no-evidence"
    report = MatrixReport(
        mode=mode,
        # Asked of the agent rather than re-derived from the environment. Reading
        # ``AIOPS_LLM_PROVIDER`` here reported "openai" for a run the agent actually
        # served with its own Anthropic default — a report that misnames the model
        # under test is worse than one that omits it, because the number gets
        # attributed to the wrong thing.
        llm_provider=f"{_rca_provider()} / {_rca_model()}",
    )
    if mode == "cold-start":
        report.notes.append(_COLD_START_NOTE)
    if mode == "learning":
        report.notes.extend([_LEARNING_NOTE, _LEARNING_SEED_NOTE])
    if mode == "poisoned-memory":
        report.notes.append(_POISON_NOTE)
    if mode == "no-evidence":
        report.notes.append(
            "no telemetry supplied: this measures the abstention contract, not accuracy. "
            "Every scenario SHOULD abstain; a confident answer here is a defect."
        )
    else:
        report.notes.append(
            "telemetry is SIMULATED from each truth file's declared observable symptoms "
            "(evals/rca_synthetic.py). No distractor noise, so accuracy here is an upper "
            "bound on live performance, not an estimate of it."
        )

    paths = discover_ecommerce_truth_files()
    if not paths:
        report.notes.append("no ecommerce truth files found")
        return report

    memory_enabled = mode in ("learning", "poisoned-memory")
    truths = [load_truth(path) for path in paths]

    total_seconds = 0.0
    with contextlib.ExitStack() as stack:
        stack.enter_context(_context_layer("on"))
        store = stack.enter_context(_scratch_store_path())
        stack.enter_context(_isolated_memory_store(store))

        seeded_by_id: dict[str, dict[str, Any]] = {}
        if memory_enabled:
            # Pass one: harvest outcomes with memory off. Doubles the run cost, which is
            # why only the memory arms pay it.
            for truth in truths:
                try:
                    harvested = _verdict_for_seeding(truth)
                except Exception as exc:
                    report.notes.append(f"{truth.get('id')}: seeding pass raised {exc}")
                    continue
                if harvested:
                    seeded_by_id[str(truth.get("id"))] = harvested

        for truth, path in zip(truths, paths, strict=True):
            scenario_id = str(truth.get("id"))
            if memory_enabled:
                _reseed_store(
                    truths,
                    seeded_by_id,
                    held_out=scenario_id,
                    poison=(mode == "poisoned-memory"),
                )
            with _memory_mode(memory_enabled):
                try:
                    score, elapsed, trace = _run_one(truth, with_evidence=with_evidence)
                except Exception as exc:  # a broken scenario must not lose the other 11
                    report.notes.append(f"{path.stem}: raised {type(exc).__name__}: {exc}")
                    continue
            total_seconds += elapsed
            report.scenarios.append(score)
            if verbose:
                mark = "PASS" if score.root_cause_correct else "MISS"
                memo = (
                    f" mem={score.memory_influence_level}"
                    f"{'/CHANGED' if score.memory_changed_ranking else ''}"
                    if score.memory_consulted
                    else ""
                )
                print(
                    f"  [{mark}] {score.scenario_id:34s} conf={score.confidence:.2f} "
                    f"status={score.status:22s} {elapsed:5.1f}s{memo}",
                    file=sys.stderr,
                )
                for line in trace:
                    print(f"         · {line}", file=sys.stderr)

    if memory_enabled and report.scenarios:
        report.notes.append(
            f"memory consulted on {report.memory_consulted_rate:.0%} of scenarios; priors "
            f"changed the top hypothesis on {report.historical_memory_influence:.0%} "
            f"(helpful {report.helpful_memory_influence_rate:.0%}, harmful "
            f"{report.wrong_memory_influence_rate:.0%}); current evidence cancelled a prior "
            f"on {report.memory_override_rate:.0%}"
        )

    if report.scenarios:
        report.notes.append(
            f"mean investigation latency {total_seconds / len(report.scenarios):.1f}s "
            f"over {len(report.scenarios)} scenario(s); token accounting needs gateway "
            "instrumentation and is not measured yet"
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RCA evaluation matrix")
    parser.add_argument("--mode", choices=MODES, default="baseline")
    parser.add_argument("--verbose", action="store_true", help="per-scenario trace on stderr")
    parser.add_argument("--out", type=Path, help="write the JSON report here as well as stdout")
    args = parser.parse_args(argv)

    if args.mode == "ablation":
        # Three arms in one run so every comparison is over the same code and the same
        # simulated evidence. The poisoned arm is included because a learning delta on its
        # own cannot distinguish "memory helps" from "memory happens to agree here".
        cold = run_matrix("cold-start", verbose=args.verbose)
        learned = run_matrix("learning", verbose=args.verbose)
        poisoned = run_matrix("poisoned-memory", verbose=args.verbose)
        payload: dict[str, Any] = {
            "mode": "ablation",
            "cold_start": cold.to_dict(),
            "learning_enabled": learned.to_dict(),
            "poisoned_memory": poisoned.to_dict(),
            "delta": {
                "learning_vs_cold": {
                    "root_cause_accuracy": round(
                        learned.root_cause_accuracy - cold.root_cause_accuracy, 4
                    ),
                    "false_positive_rate": round(
                        learned.false_positive_rate - cold.false_positive_rate, 4
                    ),
                    "brier_score": round(learned.brier_score - cold.brier_score, 4),
                },
                "poisoned_vs_cold": {
                    "root_cause_accuracy": round(
                        poisoned.root_cause_accuracy - cold.root_cause_accuracy, 4
                    ),
                    "false_positive_rate": round(
                        poisoned.false_positive_rate - cold.false_positive_rate, 4
                    ),
                },
            },
            "notes": [
                _LEARNING_NOTE,
                _LEARNING_SEED_NOTE,
                _POISON_NOTE,
                "How to read this: the learning delta is a weak signal because the seeded "
                "memories are the agent's own predictions rather than ground truth. The "
                "poisoned arm is the strong one — 'poisoned_vs_cold.root_cause_accuracy' "
                "should be ~0.0, and any drop is memory overriding current evidence.",
            ],
        }
    else:
        payload = run_matrix(args.mode, verbose=args.verbose).to_dict()

    text = json.dumps(payload, indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
