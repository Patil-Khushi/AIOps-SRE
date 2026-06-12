# Knowledge Synthesizer — PRS-007

**Phase:** Prescriptive-Adaptive · **HITL:** Required (publication is gated) ·
**KPI:** KB coverage %, agent-answer grounding rate

The "memory and documentation" agent. It triggers when an incident is
**RESOLVED** and closes the loop after the RCA agent: it turns the resolved
incident into searchable knowledge.

## Contract

`synthesize(bundle, scenario_id=None) -> SynthesisResult` (and the eval-harness
shim `run(input: dict) -> dict`).

**Input** — a resolved-incident bundle (`SynthesisInput`):
- `triage_verdict` (RA-001) and `rca_verdict` (PRS-008) — required.
- `classification` (RA-002), `ticket` (RA-003), `change_records`, `resolved_at`,
  `incident_id`, `scenario_id` — optional context.

**Output** — `SynthesisResult`: a `Postmortem`, a `RunbookSuggestion`
(new/update), and a `KBArticle` persisted as `pending_review`, plus the
`dedup` decision and a redaction summary.

## Pipeline (v0)

1. **Idempotency** — `find_kb_by_incident_id` guards against synthesizing the
   same incident twice (`dedup_action = skip_idempotent`).
2. **Timeline** — reconstructed from the cross-agent `audit_metadata.created_at`
   timestamps. RCA carries no timeline field, so we assemble it rather than
   change RCA's contract.
3. **Postmortem** — LLM draft via `aiops.llm` with a **deterministic fallback**
   (stub provider / CI) built from the same structured inputs.
4. **Runbook suggestion** — reads the runbook library (`aiops.runbooks`); if a
   runbook exists for the service it suggests an `update`, else a `new` one.
   The body follows the locked runbook structure and is derived from RCA's fix
   steps. *Suggested, not written* — writing happens only after HITL approval.
5. **Redaction** — `redaction.redact` scrubs PII/secrets before persisting.
   ⚠️ **Best-effort POC redactor** (regex-only, no dependency, no I/O): catches
   common shapes (emails, IPs, tokens, AWS keys, JWTs, PEM keys, `key=secret`).
   It is **not** a compliance-grade scrubber — don't rely on it as the sole
   control for regulated data.
6. **Quality score + dedup** — cosine nearest-K when embeddings are available
   (`nearest_kb_articles`), signature overlap otherwise. A near-duplicate
   yields `dedup_action = duplicate` and no new row.
7. **Persist** — a new article is saved as `pending_review`. **Publication is
   platform-HITL-gated and live in this PR**: the `seam.knowledge.publish` tool
   ([aiops/tools/knowledge.py](../../aiops/tools/knowledge.py)) is registered
   under the **Required** `knowledge.publish` capability
   ([aiops/policy/gate.py](../../aiops/policy/gate.py), mirrored in
   [policies/hitl.rego](../../policies/hitl.rego)). The gate enforces approval
   at the registry boundary — the agent drafts to `pending_review` and
   **physically cannot self-publish**; only `request_publish` after a human
   approval flips an article to `published` and writes its runbook.

## Decoupling

The synthesizer runs *after* the incident is resolved and only ever writes new
rows/files. If it fails or is slow, the Triage→…→RCA pipeline is unaffected
(enforced at the trigger boundary in the demo server).

## Seams used

| Need | Seam |
|---|---|
| LLM | `aiops.llm.complete` |
| KB persistence + dedup/RAG | `aiops.state.repository` (`*_kb_*`) |
| Runbook library | `aiops.runbooks` |
| PII/secret redaction | `agents.knowledge_synthesizer.redaction` |

## Run it

```bash
uv run python -m agents.knowledge_synthesizer bundle.json
uv run python -m evals.harness --agent knowledge_synthesizer
```
