# `perf_reliability/` — UC3: Predictive Infrastructure & Code Reliability

> **Client track (Azure Databricks). Recommend-only.** Point it at a slow
> pipeline; it names the slowest notebooks and gives line-level optimization
> recommendations, each with an estimated saving + implementation-complexity
> rating. It **never** changes code or reruns anything — a human decides.

## Contract

- **Input** (`PerfInput`): `job_name`, `total_runtime_minutes`, and a list of
  `notebooks` (each: `path`, `source`, `runtime_minutes`, `is_child`, `called_by`).
- **Output** (`PerfVerdict`): `summary`, `bottleneck_assets`, ranked `findings`
  (`notebook`, `line`, `snippet`, `issue`, `recommendation`, `estimated_saving`,
  `implementation_complexity`), `primary_recommendation`, `confidence_score`.
- Entry points: `analyze(perf_input) -> PerfVerdict`, and the eval-harness shim
  `run(input: dict) -> dict`.

## How it works

1. Rank assets by measured runtime (slowest first).
2. **LLM pass** (`aiops.llm`, Azure by default) reads the source + runtimes and
   returns findings — this is the real brain. Prompt lives in `prompts.py`.
3. **Deterministic heuristic fallback** (`agent.py::_ANTIPATTERNS`) runs when the
   LLM is the stub (CI), unparseable, or errors. It scans for known anti-patterns
   (`coalesce(1)`, driver `collect()`/loops, repeated actions) so the agent always
   produces a useful, testable verdict offline.

## Run it

```bash
# End-to-end on the bundled sample (no Azure needed) — fetches via the tool
# seam, then analyzes:
uv run python -m agents.perf_reliability

# Evals (offline heuristic path; keeps CI green):
uv run python -m evals.harness --agent perf_reliability
```

## Ownership (first demo, 4 × 6h)

| Person | Files | Task |
|---|---|---|
| **A — Brain** | `prompts.py` | Tune the LLM prompt + JSON contract; grow `evals/golden.json` |
| **B — Data** | `aiops/tools/databricks/`, `demo/uc3_sample/` | Make the sample realistic; write the **live Databricks provider** behind `code.assets.fetch` |
| **C — Shape + Screen** | `agent.py`, UI wiring | Surface `PerfVerdict` on a clean screen |
| **D — Glue + Demo** | `evals/`, demo script | Integrate, add cases, rehearse |

## Going live (swap, not rebuild)

The agent takes data **directly** — it does not know or care where it came from.
The demo fetches from the file-backed `sample` provider
(`code.assets.fetch`, provider `sample`). To go live, add a `databricks`
provider for the same capability (Databricks Jobs API for runtimes +
Workspace API for notebook source) and `select_provider("code.assets.fetch",
"databricks.code.assets.fetch")`. No agent change.

## Open question that bounds fidelity

How are **per-child-notebook runtimes** obtained from Databricks (job task
timeline vs. Spark query history vs. cell timing)? That decides how precisely we
can attribute the bottleneck. Confirm with the client before the live swap.
