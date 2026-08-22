# Runbook Executor — RA-004

Reactive-Active. Answers exactly one question: **do we have a known, approved, safe
procedure for this incident?** It finds candidate runbooks, ranks them with a
deterministic explainable score, lets an SRE choose among the *eligible* ones,
re-validates whatever was chosen, dry-runs it, executes it **once** behind the platform
HITL gate, rolls back on failure, and hands the result to the Resolution Verifier.

It does **not** determine root cause (that is `agents/rca_agent/`) and it does **not**
decide whether the incident recovered (that is `agents/resolution_verifier/`). The
strongest thing it can report is `EXECUTED` + `next_action=VERIFY`.

| | |
|---|---|
| Catalog ID | RA-004 |
| Phase | Reactive-Active |
| HITL level | **Required** for destructive steps (`automation.runbook.execute`); **None** for the dry run (`automation.runbook.simulate`) and non-destructive steps (`automation.runbook.apply`) |
| Inputs | `IncidentContext` (service, environment, severity, alert, failure category, observed signals, incident lifecycle) — or the legacy `Incident` hand-off |
| Outputs | `DiscoveryResult` (ranked candidates) · `PlanResult` (dry run + reserved execution) · `ExecutorResult` (§27 contract + verifier handoff) · `RunbookExecution` (legacy) |
| LLM | **none** — matching, risk and validation are pure functions. `tests/test_runbook_boundaries.py` enforces it |
| KPI | auto-remediation success %, rollback incidents, verification pass rate after execution |

## The flow

```
discover_candidates(ctx)          matching.py     rank every service-scoped runbook
  ├─ 1 applicable   -> AUTO_SELECT (the platform may proceed)
  ├─ >1 applicable  -> CANDIDATES  (an SRE picks which to evaluate)
  ├─ 0, none fit    -> NOT_APPLICABLE / NO_RUNBOOK -> RCA
  └─ undeterminable -> AMBIGUOUS   (never executes)
        │
plan_execution(ctx, runbook_id?)  applicability.py + actions.py + risk.py + dryrun.py
  ├─ lifecycle check     ACTIVE + approved_by, or refused
  ├─ applicability       service / environment / category / alert / signals / severity
  ├─ prerequisites       incident still open, steps in scope, alert firing, signals
  ├─ action resolution   every step must resolve in the action registry
  ├─ parameter check     type, range, scope, no command-shaped values
  ├─ risk                LOW / MEDIUM / HIGH / CRITICAL (+ autonomy class)
  └─ DRY RUN             READY -> an execution is reserved | BLOCKED -> nothing runs
        │
execute_plan(plan, ctx)           execution_state.py + agent.run_plan
  ├─ duplicate?          return the existing execution, do NOT re-run  (§20)
  ├─ re-validate         incident may have closed or aged out since planning (§24)
  ├─ lease               one remediation at a time per namespace/service (§25)
  ├─ run_plan            the v0 step loop: simulate -> gated execute -> rollback
  └─ persist + hand off  EXECUTED + next_action=VERIFY + VerificationHandoff (§29)
```

## Two generations of entry point

Both are supported and both are tested; the v0 surface is unchanged.

| | v0 (legacy) | production |
|---|---|---|
| Entry | `execute_runbook(Incident)` / `run_plan` / `select` | `discover_candidates` → `plan_execution` → `execute_plan` (or `execute`) |
| Selection | best substring match on service + tags + severity | ranked candidates over seven facets, service-scoped |
| Result | `RunbookExecution` (`resolved` / `denied` / `rolled_back` / `failed` / `no_runbook`) | `ExecutorResult` (`EXECUTED` / `NO_RUNBOOK` / `NOT_APPLICABLE` / `AMBIGUOUS` / `BLOCKED` / `FAILED` / `ROLLED_BACK`) + `next_action` |
| State | in the returned object only | durable row in `runbook_executions`, idempotent by (incident, runbook, version, plan hash) |
| Used by | the CLI, `POST /api/demo/runbook-executor/run`, the eval harness | `demo/ui/runbook_routes.py`, the dashboard |

`ExecutorResult.legacy` carries the v0 object verbatim, so a consumer of either shape
keeps working.

## The match score

`score = (earned + specificity) / (comparable + max_specificity)` — *"of the things we
could actually compare, how much matched"*. A facet counts only when the runbook
declares a constraint **and** the incident supplied the fact; anything else is excluded
from both halves rather than counted as a match or a miss. Weights live in
`matching.FACET_WEIGHTS` and every point comes back as a `ScoreComponent`, so the total
can be audited rather than trusted.

Consequence worth knowing: a runbook that constrains nothing (a generic per-service
recovery) scores well because it *does* match everything it claims. That is why
specificity is in the numerator, and why automatic selection keys off **applicability**,
not score — a high score is never on its own a licence to execute.

## Runbook lifecycle (§9/§10)

Frontmatter carries `version`, `status`, `owner`, `approved_by`, and optionally
`source_incident` / `change_reason` / `previous_version`. Only
**`status: active` with a recorded `approved_by`** may execute; `draft`,
`pending_review`, `approved`, `superseded`, `archived` and `rejected` are all refusals,
and a runbook with no status parses as `draft` (fail-closed, same spirit as
`RunbookStep.destructive = True`). A refused runbook is still *listed* — the operator
sees it with its reason instead of it silently disappearing.

A Knowledge Synthesizer proposal therefore cannot reach the executor: it has to be
reviewed by a human into a new ACTIVE version of an executable runbook first. The
executor never imports `aiops.runbooks` (the descriptive library KS writes into) —
`tests/test_runbook_boundaries.py` checks that.

## The library

Executable runbooks are markdown + YAML frontmatter under [`runbooks/`](runbooks/),
**generated** by `scripts/generate_runbooks.py` from one table so the runbook, the
scenario YAML, the truth file and the Prometheus alert cannot drift. Point
`AIOPS_RUNBOOK_EXECUTOR_DIR` at another directory to override.

Regenerate after editing that table:

```powershell
uv run python scripts/generate_runbooks.py
```

## Modules

| File | Responsibility |
|---|---|
| `agent.py` | entry points + the step loop (the only place a step is dispatched) |
| `library.py` | frontmatter parsing, version/lifecycle, `is_executable` |
| `applicability.py` | facet verdicts + prerequisite evaluation (pure) |
| `matching.py` | scoring, ranking, the §6 decision rules (pure) |
| `actions.py` | the action registry: what a step may do, and parameter validation |
| `risk.py` | deterministic LOW/MEDIUM/HIGH/CRITICAL + autonomy class (pure) |
| `dryrun.py` | the READY/BLOCKED gate + `plan_hash` |
| `execution_state.py` | state machine, idempotency key, leases, step timeout/retry policy |
| `results.py` | `PlanResult` / `ExecutorResult` / `VerificationHandoff` |
| `events.py` | append-only audit log + secret redaction |
| `metrics.py` | in-process counters and rates (§31) |
| `selector.py` | the v0 selector, kept as a thin wrapper over `matching` |

## Configuration

All read *inside* the function, never at import (so `monkeypatch` works):

| Var | Default | Effect |
|---|---|---|
| `AIOPS_RUNBOOK_EXECUTOR_DIR` | shipped `runbooks/` | library directory |
| `AIOPS_RUNBOOK_MAX_INCIDENT_AGE_MINUTES` | 240 | stale-incident cutoff (§24) |
| `AIOPS_RUNBOOK_HITL_RISK_THRESHOLD` | `HIGH` | risk level at which a human is demanded regardless of the step's own flag |
| `AIOPS_RUNBOOK_STEP_TIMEOUT` | 60s | per-step timeout; gated steps add the approval window |
| `AIOPS_RUNBOOK_MAX_RETRIES` | 1 | retries — applied **only** to `retry_safe` actions, never to a gated call |
| `AIOPS_RUNBOOK_RETRY_BACKOFF` | 0.5s | base backoff |
| `AIOPS_RUNBOOK_STEP_BREAKER_SECONDS` | 30s | circuit breaker per action |
| `AIOPS_RUNBOOK_EXECUTION_TIMEOUT` | 900s | overall execution budget |
| `AIOPS_RUNBOOK_LEASE_SECONDS` | 900s | concurrency lease TTL (§25) |
| `AIOPS_ENVIRONMENT` (read by the API layer) | `demo` | environment reported to the matcher and risk model |

## Safety invariants, and where they are enforced

| Invariant | Enforced by |
|---|---|
| Only ACTIVE + approved runbooks execute | `library.is_executable`, `tests/test_runbook_applicability.py` |
| An SRE's choice cannot bypass validation | `plan_execution` re-validates; `tests/test_runbook_routes.py` |
| No arbitrary command can be executed | `actions.py` (closed action registry + injection refusal), `tests/test_runbook_action_safety.py` |
| A step cannot reach a service the runbook did not declare | `actions.target_in_scope` |
| A disruptive action cannot route around the gate | `actions.validate_step` (`destructive: false` is refused) |
| CRITICAL risk never auto-executes | `risk.assess_plan` → dry run BLOCKED |
| A BLOCKED dry run cannot be executed | `dryrun` + `PlanResult.ready` |
| The same plan cannot run twice | `idempotency_key` + a unique DB index |
| A stale incident cannot be remediated | `applicability` prerequisite, re-checked at execute time |
| Two executions cannot remediate one service at once | `runbook_leases` unique index |
| A failed step stops the run | `run_plan` (unchanged from v0) |
| Rollback passes the same gate | `run_plan._rollback` via `automation.runbook.execute` |
| The executor never declares recovery | `ExecutorStatus` has no such value; `tests/test_runbook_boundaries.py` |
| No LLM, no shell, no RCA import | `tests/test_runbook_boundaries.py` (AST) |

## Run locally

```powershell
# Eval pass (no cluster, no LLM): legacy execution + discovery decisions
uv run python -m evals.harness --agent runbook_executor

# CLI demo — gate blocks the destructive step (expect "denied"):
uv run python -m agents.runbook_executor --service payment --tags crash,oom --no-approve

# CLI demo — happy path (a background thread approves):
uv run python -m agents.runbook_executor --service payment --tags crash,oom --auto-approve-after 1

# Tests
uv run pytest tests/test_runbook_executor.py tests/test_runbook_executor_audit.py `
              tests/test_runbook_matching.py tests/test_runbook_applicability.py `
              tests/test_runbook_action_safety.py tests/test_runbook_dryrun.py `
              tests/test_runbook_execution_state.py tests/test_runbook_boundaries.py `
              tests/test_runbook_routes.py
```
