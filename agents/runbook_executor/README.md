# Runbook Executor — RA-004

Reactive-Active. Selects and executes the appropriate runbook for a **classified
incident** with step-level guardrails: a dry-run preview, a platform HITL gate on
destructive steps, autonomous execution of non-destructive steps, and automatic
rollback on failure — producing an audit-grade execution log.

| | |
|---|---|
| Catalog ID | RA-004 |
| Phase | Reactive-Active |
| HITL level | **Required** for destructive steps (`automation.runbook.execute`); **None** for preview (`automation.runbook.simulate`) and non-destructive steps (`automation.runbook.apply`) |
| Inputs | `Incident` = `{ service, severity?, tags[], incident_id? }` (the RA-002 hand-off) + the runbook library + approvals |
| Outputs | `RunbookExecution` — selected runbook, per-step execution log, resolution `status`, rollback artifacts — see [models.py](models.py) |
| LLM | none — selection is deterministic substring matching (no RAG/embeddings/DB) |
| KPI | auto-remediation success %, rollback incidents |

## How it works

```
select runbook (service + tags + severity)        selector.py
  └─ none?  -> RunbookExecution(status="no_runbook")
dry-run preview every step                        automation.runbook.simulate  (NONE)
for each step in order:
    destructive?  -> automation.runbook.execute   (REQUIRED — platform HITL gate)
    otherwise     -> automation.runbook.apply      (NONE — autonomous)
    gate blocked  -> status="denied", stop
    tool failed   -> roll back prior steps in reverse, status in {rolled_back, failed}
all ok -> status="resolved"
```

The agent owns **policy** (which runbook, which step is destructive, the rollback
order). The platform owns **mechanism**: HITL is enforced at the
`aiops.tools.get_registry().call(...)` boundary, never inside the agent
(CLAUDE.md #3). A buggy RA-004 still cannot run a destructive step without an
approver — the gate physically blocks it.

### Resolution statuses

| status | meaning |
|---|---|
| `resolved` | every step executed |
| `denied` | a destructive step was refused at the HITL gate; nothing past the gate ran |
| `rolled_back` | a step failed; previously executed steps were undone cleanly |
| `failed` | a step failed **and** a rollback step also failed (manual intervention) |
| `no_runbook` | no library runbook matched the incident |

## The runbook library

Executable runbooks are markdown files with YAML frontmatter (selection metadata
+ structured `steps`, each carrying `destructive` / `rollback_action`) under
[`runbooks/`](runbooks/). They are parsed by [library.py](library.py); point
`AIOPS_RUNBOOK_EXECUTOR_DIR` at another directory to override.

> **Why a local library, not `aiops.runbooks`?** The shared platform library
> (`aiops/runbooks/`) stores human-authored prose runbooks (and the Knowledge
> Synthesizer's suggestions) but its `Runbook` model has no executable `steps`
> field yet. RA-004 needs structured steps, so the executable definitions live
> here. Promoting them into `aiops.runbooks` (adding a `steps` field) is the
> post-POC integration step.

## Run locally

```powershell
# Eval pass against the v0 goldens (no approver -> destructive steps "denied")
uv run python -m evals.harness --agent runbook_executor

# CLI demo — gate blocks the destructive step (expect "denied"):
uv run python -m agents.runbook_executor --service payment --tags crash,oom --no-approve

# CLI demo — happy path (a background thread approves; destructive step runs):
uv run python -m agents.runbook_executor --service payment --tags crash,oom --auto-approve-after 1

# Tests
uv run pytest tests/test_runbook_executor.py
```
