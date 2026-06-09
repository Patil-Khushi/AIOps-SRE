# Eval Methodology — how agents are graded

"RCA pass rate 1.0" means nothing without saying *what* 1.0 measures and *how* the score is
computed. This document makes the eval harness credible to an outside reader. The engine is
[`evals/harness.py`](evals/harness.py); the scoring is [`evals/scoring.py`](evals/scoring.py);
the per-agent how-to is [`evals/README.md`](evals/README.md). This is the methodology layer on
top of those. It exists because CLAUDE.md non-negotiable #7 makes the eval harness a day-one
requirement, not a nice-to-have — "looks good in the demo" is not a metric.

> **Read this first (the honest caveat).** The pass rates in §7 are measured against each
> agent's **deterministic fallback path** — rule-based dedup, template summaries, Tier-4
> keyword classification, the RCA deterministic fallback — because CI and this dev environment
> run without a live LLM SDK installed. A 1.0 here means "the agent's non-LLM logic is correct
> for these inputs," **not** "the LLM produces a perfect answer." Real-LLM pass rates will
> differ and are expected to be lower; closing that gap is tracked in the
> [Risk Register](RISK_REGISTER.md) ("Real-LLM eval pass rate drops below the stubbed 1.0").

---

## 1. What an eval looks like

The harness runs **two complementary sources of truth**:

**(a) Agent goldens — `agents/<name>/evals/golden.json`.** Catch regressions in one agent's
output for known inputs. Each file is a list of cases (or a `{"cases": [...]}` wrapper with
top-level metadata). A `Case` is:

```json
{
  "id": "sev1-customer-facing",
  "description": "Customer-facing 5xx spike → Sev-1",
  "input":    { "alert": { "...": "the dict passed to the agent's run()" } },
  "expected": { "severity": "Sev-1", "min_confidence": 0.7 },
  "tags": ["severity"]
}
```

The harness imports `agents/<name>/agent.py::run(input: dict) -> dict`, calls it per case (after
an optional `reset_state()` for agents with persisted state), and scores `actual` against
`expected`.

**(b) Demo truth files — `demo/truth_files/<scenario>.yaml`.** One YAML per failure scenario
(see [ADR-007](docs/adr/0007-truth-files-vs-db.md)), declaring the real cause, the expected RCA,
and the correct fix steps. A truth file opts in to **automated** scoring (EVAL-1, issue #75) by
adding an `expected_alert_payload` block (the synthetic `Alert` the scenario produces) and an
`exercises` block (which agents to run, with their expected output). The smoke test enforces
that every scenario *has* a truth file; the `exercises` block is what makes it *graded*.

---

## 2. Scoring rules

Scoring is intentionally tiny and transparent ([`evals/scoring.py`](evals/scoring.py)). Each
`expected` block is a **flat dict** whose keys encode both the target field and the check via a
suffix grammar:

| Key form | Check | Example |
|---|---|---|
| `<field>` | exact equality | `"incident_type": "application"` |
| `<field>_in: [..]` | membership | `"severity_in": ["Sev-1","Sev-2"]` |
| `<field>_contains: v` | substring (str) / element (list) | `"decision_trace_contains": "CMDB"` |
| `min_<field>: n` | numeric `>=` | `"min_confidence": 0.7` |
| `max_<field>: n` | numeric `<=` | `"max_latency_ms": 1000` |

- **Per-case verdict:** a case `passed` only when **every** check in its `expected` block
  passes. There is no per-check weighting.
- **Partial credit:** `score` is reported as the *fraction* of checks that passed (e.g. 3/4 =
  0.75) for diagnostics, but **partial credit does not make a case pass** — `passed` requires
  all checks. Pass-rate math (§3) counts whole cases, not partial scores.
- **Per-agent custom checks:** the grammar is deliberately minimal. Add a new check type to
  `scoring.py` only when a real agent forces it — don't pre-build for hypotheticals.
- **Errors fail closed:** if an agent raises, the case is scored `passed=False, score=0.0` with
  the exception in `details`.

---

## 3. Pass-rate definition

- **Per agent:** `pass_rate = (# cases passed) / (# cases)`. An agent with **no** golden file
  scores `1.0` (nothing to regress) — e.g. `auto_healer_lite`, the HITL-demo agent, ships no
  goldens.
- **Per truth file:** same fraction over its runnable `exercises`; a file with no opted-in
  `exercises` contributes a neutral `1.0` (it isn't penalized for not yet being wired).
- **Overall:** the unweighted mean of every agent run's and truth-file run's `pass_rate`.
- **Ship threshold:** **0.85** — the bar an agent must clear to ship (per
  [`DEMO_PLAN.md`](DEMO_PLAN.md)). The RCA Agent has a phased bar: **≥ 0.6 in W1**, **≥ 0.85 in
  W2** after prompt tuning.

> **Known weighting caveat:** because truth files without `exercises` contribute `1.0`, and
> most of the 15 current truth files haven't opted in to scoring yet, the **overall** number is
> optimistic — it averages real agent grades with neutral 1.0s. Read the per-agent §7 numbers,
> not just the headline overall.

---

## 4. CI gating (already exists)

Every PR runs the gate in [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```
uv run python -m evals.harness --ci --min-pass-rate 0.85
```

In `--ci` mode the harness prints the JSON summary and **exits non-zero when the overall pass
rate is below the threshold**, which **blocks the merge**. This is the load-bearing control:
a prompt change that regresses a golden set cannot merge silently. Scopes:

```
uv run python -m evals.harness                      # all agents + all truth files
uv run python -m evals.harness --agent alert_triage # one agent's goldens
uv run python -m evals.harness --truth-files-only   # only scenario truth-file evals
```

---

## 5. Champion / challenger

The principle (CLAUDE.md #6, #7): **a prompt change is a model change** — every model, prompt,
and policy is versioned and re-evaluated before it becomes the new "champion."

- **Today (POC, manual):** the champion is whatever is on `main`. A challenger (new prompt or
  pinned model) runs the *same* golden + truth-file sets on its branch; CI's 0.85 gate is the
  promotion bar. A challenger that drops any agent below threshold fails the PR and does not
  merge. Model versions are pinned (never `latest`, see [ADR-003](docs/adr/0003-default-llm-provider.md))
  so a provider-side rotation can't silently swap the champion.
- **Post-POC (automated, planned):** shadow-eval the challenger against live traffic, compare
  to the champion on the same cases, and auto-promote only on a non-regression — with automatic
  rollback if a regression is detected after promotion. The closed-loop machinery
  (`feedback.promote_model` is already a Required-HITL capability in the gate) is the seam this
  plugs into; the automation itself is not built in the POC.

---

## 6. Drift detection (post-POC plan)

The POC grades against deterministic fallbacks; production must catch *silent* regressions in
live-LLM output. Planned monitoring:

- **Real-LLM eval runs on a schedule** (not just CI's stub path), tracking pass rate over time
  per agent; alert on a downward step change.
- **The stub-vs-real gap is the #1 drift signal.** A 1.0 stub rate with a falling real rate is
  exactly the failure mode the Risk Register names — make that delta a tracked metric, not a
  surprise on demo day.
- **Truth-file expansion:** opt more `demo/truth_files/*.yaml` into the `exercises` path so the
  scenario-level evals carry real signal instead of neutral 1.0s.
- **Champion drift:** re-run the champion against its own goldens periodically to catch
  provider-side model drift even with pinned versions.

---

## 7. Per-agent eval status

Measured by `uv run python -m evals.harness` on 2026-06-09, against the **deterministic
fallback paths** (no live LLM SDK in CI / this env — see the caveat at the top):

| Agent | Cases | Pass rate | Notes |
|---|---|---|---|
| `alert_triage` (RA-001) | 8/8 | 1.00 | Dedup, severity, CMDB hit/miss, multi-source. |
| `incident_classifier` (RA-002) | 5/5 | 1.00 | Five incident types; rule-based first pass (LLM consult skipped on fallback). |
| `auto_ticketing` (RA-003) | 5/5 | 1.00 | Suppression, urgency mapping, ticket record. |
| `notification_router` (RA-005) | 6/6 | 1.00 | Severity/time-of-day routing. |
| `rca_agent` (PRS-008 ★) | 1/1 | 1.00 | Single scenario (`slow-product-catalog`); deterministic fallback (anthropic SDK absent). RCA's real bar is ≥0.85 W2 against the live model. |
| `auto_healer_lite` | 0/0 | 1.00 (n/a) | HITL-demo agent; no golden set — exercised by the HITL approval flow, not goldens. |
| Truth files (×15) | — | 1.00 | Presence enforced by smoke test; automated **scoring** is opt-in (EVAL-1) and most files contribute a neutral 1.0. |
| **Overall** | **25 graded cases** | **1.00** | Optimistic — see the §3 weighting caveat. |

The honest reading: **the agents' non-LLM logic is well-covered and green; the LLM-dependent
quality (RCA narrative, ambiguous-severity classification) is not yet graded against a live
model.** That is the next eval-credibility milestone, not a number the POC can claim today.

---

## References

- [`evals/harness.py`](evals/harness.py) — the engine (discovery, run, CI mode).
- [`evals/scoring.py`](evals/scoring.py) — the check grammar in §2.
- [`evals/README.md`](evals/README.md) — per-agent golden how-to.
- [`demo/truth_files/`](demo/truth_files/) — ground-truth scenario files (+ `template.yaml`).
- [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — the CI gate cited in §4.
- [`DEMO_PLAN.md`](DEMO_PLAN.md) — the 0.85 ship threshold and RCA phased bar.
- [`CLAUDE.md`](CLAUDE.md) — non-negotiable principles #6 (closed-loop) and #7 (evals from day one).
- [ADR-003](docs/adr/0003-default-llm-provider.md), [ADR-007](docs/adr/0007-truth-files-vs-db.md) — the provider and truth-file decisions behind this methodology.
