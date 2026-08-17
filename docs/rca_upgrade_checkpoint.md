# RCA Agent Upgrade — Phase Checkpoint

Resumable state for the multi-phase work turning PRS-008 from a one-shot LLM
classifier into an evidence-driven SRE investigator.

**Status: Phases 1-7 complete. All locked phases done — full suite green (2604 passed, 0 failed).**

Keep this file current at the end of each phase. It exists so the next session can
resume without re-deriving the decisions, and so a claim about what is measured can
be checked against what was actually run.

---

## Locked decisions

Settled with the owner; do not relitigate without a new decision recorded here.

| # | Decision |
|---|---|
| Q1 | **Evaluation first.** A real baseline before behaviour changes. |
| Q2 | **Replace** the hardcoded failure-key / injection-mechanism list in the prompt with a runtime-derived action vocabulary. RCA may know the approved *actions*; never the injection mechanism, env var names, or chaos details. |
| Q3 | **Leave ServiceNow auto-close as-is.** No expansion in this work. |
| — | Truth-file corpus must never be RCA historical memory. Memory is outcome-backed only. |
| — | Current evidence outranks historical memory, enforced in **deterministic scoring**, not prompt text. |
| — | Memory lifecycle `NEW → UNVERIFIED → VERIFIED → TRUSTED → SUPERSEDED/INVALIDATED`. A prediction alone never becomes trusted. |
| — | **RCA memory comes from an allowlist of outcome-backed providers, never from the history chain.** Added in Phase 3 — the chain's default searches the truth files, which are the evaluation's answer key. |
| — | Exactly **one LLM call** on the normal path. |
| — | No vector DB, no new infrastructure (ADR-006 stands). |
| — | `run()` performs no I/O. |

## Phase order

0. Read-only analysis — **done**
1. Evaluation baseline + data contracts + tests — **done**
2. Deterministic investigation pipeline — **done**
3. Historical outcome memory + retrieval — **done**
4. LLM prompt / synthesis update (includes the Q2 prompt change) — **done**
5. Blast radius + recovery/risk integration — **done**
6. Post-recovery outcome recording + learning — **done**
7. Full evaluation + ablation — **done**

---

## Phase 1 — what landed

### Created

| File | Role |
|---|---|
| `agents/rca_agent/investigation/__init__.py` | package docstring: the stage pipeline and why it lives in the agent, not `aiops/` |
| `agents/rca_agent/investigation/models.py` | every Phase-1 data contract; **types only, no logic** |
| `evals/rca_truth.py` | **the blindness boundary** — truth file → blind agent input, with the guard on the production path |
| `evals/rca_synthetic.py` | simulated telemetry from `expected_signals`, plus a coverage account of what RCA cannot observe |
| `evals/rca_metrics.py` | metric scorers + `PENDING_METRICS` |
| `evals/rca_eval.py` | accuracy CLI: `baseline` / `no-evidence` / `cold-start` / `learning` / `ablation` |
| `tests/test_rca_eval_blindness.py` | 69 cases — truth cannot reach RCA, and the grading key still carries it |
| `tests/test_rca_investigation_models.py` | 27 cases — negative evidence, memory lifecycle, budget, blast radius, risk |
| `tests/test_rca_fallback_honesty.py` | 49 cases — abstention, confidence cap, provider resolution, back-compat |

**145 new tests, all passing.**

### Modified

| File | Change |
|---|---|
| `agents/rca_agent/agent.py` | removed the service-name fallback; `offline=` retrieval suppression; per-call provider resolution; evidence-bounded confidence; `root_cause_status` derivation; corrected module docstring |
| `agents/rca_agent/models.py` | additive optional `root_cause_status`, `llm_stated_confidence` |
| `agents/rca_agent/evals/golden.json` | 1 stale case → 12 generated blind cases |
| `evals/scoring.py` | new `<field>_contains_any: [...]` check |
| `evals/harness.py` | per-agent truth-file input adapters; `rca_agent` runnable |
| `tests/test_incident_commander.py` | monkeypatched private constant → `setenv` (now works) |
| `demo/ecommerce/truth_files/order_service_postgres_down.json` | note recording why no `rca_agent` exercise block |

### Four real defects fixed

1. **The service-name fallback.** `scenario_id == ... or service in _LOCKED_SERVICES` meant *any* user-service incident with no usable LLM returned a hand-written MySQL cause at confidence 0.85 — for a service with four distinct failure modes. The set is deleted; the `scenario_id` branch (the rehearsed demo path) is preserved.
2. **The provider pin defeated the CI stub.** `get_provider` lets an explicit argument beat `AIOPS_LLM_PROVIDER`, so RCA asked Anthropic even under `stub`, failed on credentials, and took the *exception* fallback. CI was exercising an error path while appearing to exercise the stub, and the `[stub]` branch was dead code. Now resolved per call, deferring to an explicit `stub`.
3. **`run()` was not zero-I/O** despite its docstring. It reached `_evidence.gather` — ~14 registry calls, HTTP round-trips against a real `AIOPS_PROMETHEUS_URL`. Growing the golden from 1 to 12 cases pushed `test_main_summary_includes_both_buckets` past the 60s cap with ~170 HTTP calls. `offline=True` fixes it and makes the goldens environment-independent.
4. **Unbounded confidence on no evidence.** The model's figure passed through verbatim. Now capped at 0.3 when nothing was observed, with the cap recorded in the decision trace, and `llm_stated_confidence` kept for calibration.

---

## Phase 1 — test results

Full suite, `uv run pytest -q -m "not integration and not llm"`:

```
2276 passed · 1 skipped · 2 xfailed · 0 failed · 0 errors   (exit 0)
```

The 1 skip and 2 xfails are pre-existing and unrelated. `ruff check` and
`ruff format --check` clean across 387 files. mypy: 42 pre-existing errors elsewhere
in the tree, **0** in `agents/rca_agent`.

145 of those tests are new in Phase 1.

## Phase 1 — measurements actually run

Local, `AIOPS_RCA_LLM_PROVIDER` unset → agent default **anthropic / claude-sonnet-4-6**. Not CI.

### Golden pass rate

| | Before | After |
|---|---|---|
| `rca_agent` goldens | **0.0** (1 stale case: asserted `productCatalogFailure` on a deleted service; the live LLM correctly answered *"not a service that exists"* at 0.1) | **1.0** (12/12) |

The two numbers measure different things — the before-case was invalid, not failed. The
honest reading is *"there was no usable baseline"*, not *"accuracy went from 0% to 100%"*.

### Accuracy matrix — `--mode baseline`, simulated telemetry

| Metric | Value |
|---|---|
| root-cause accuracy | **12/12** |
| service accuracy | 12/12 |
| remediation accuracy | 11/12 |
| false-positive rate | 0.0 |
| abstention rate | 0.0 |
| HITL safety | 1.0 |
| evidence coverage | 0.876 |
| Brier score | 0.0099 |
| over/under-confidence | 0.0 / 0.0 |
| mean latency | 8.4 s/scenario, 1 LLM call |

### Abstention contract — `--mode no-evidence`, stub, fully offline

| Metric | Value |
|---|---|
| abstention rate | **1.0** |
| root-cause accuracy | 0.0 |
| false-positive rate | 0.0 |
| max confidence | 0.2 |
| statuses | all `insufficient_evidence` |

### Why 12/12 must not be quoted as production accuracy

Four reasons, all structural:

1. **No distractor noise.** `expected_signals` lists only the discriminating signals; real telemetry buries them among unrelated series. This is an **upper bound**.
2. **Generous grading.** `match_any_keyword` accepts any one synonym, so "database connection" scores as correct for the Postgres outage.
3. **Alert names are informative.** `EcommercePostgresDown` nearly names the answer. Realistic, but it makes the task easier than diagnosing from raw telemetry.
4. **Gauges are decisive by construction.** The simulator emits UNREACHABLE for exactly the failing store and REACHABLE for the other two.

Status of this number: **simulated**, not production-validated. Nothing here demonstrates
the 85–89% target on live incidents, and it should not be reported as if it did.

---

## Phase 1 — findings for later phases

### Evidence coverage gaps — RCA issues no query for these

From `SyntheticEvidence.unrepresentable`, aggregated across the 12 scenarios:

| Signal | Scenarios | Note |
|---|---|---|
| `payment_failures_total{reason=...}` | 3 | `gateway_timeout`, `injected_500`, `redis_error` — **caused the one remediation miss** |
| `trace` | 2 | no trace query at all |
| `up` | 1 | scrape failure not observable |
| `orders_created_total` | 1 | throughput not observed |
| `login_failure_total` | 1 | |

Aggregate coverage **0.876** — about 12% of declared observable signals are outside
RCA's query set.

**The redis_down miss is the most instructive result in the whole run.** RCA proposed
`payment_service.dns_failure` instead of `payment_service.redis_down`. It reasoned
*correctly* by its own prompt: V6 says a genuine Redis outage shows
`payment_failures_total reason=redis_error`, and RCA has no query for
`payment_failures_total` — so it found no `redis_error`, and concluded DNS. **A sound
inference from missing evidence.** The fix is a query, not a prompt tweak.

**Resolved in Phase 2:** `sum by (reason) (rate(payment_failures_total[5m]))` was added
to `evidence.error_breakdown`. Coverage rose 0.876 → 0.927 and remediation accuracy
0.917 → 1.0, with `payment_service_redis_down` now diagnosed correctly.

### Carried forward

- **Prompt still hardcodes 13 failure keys and injection mechanisms** (`INJECT_LATENCY_SECONDS`, `MYSQL_HOST unresolvable`). Q2 says replace with a runtime-derived vocabulary — **Phase 4**, needs its own before/after eval since V6 is tuned.
- **`scenario_id` remains an injection-truth channel** into `_fallback_verdict`. The evaluation never passes it, but the demo path does. **Resolved after Phase 7: kept, documented as demo-only** — see the dedicated section near the end of this file for the full argument.
- **`flag_for_service` is dead** — returns a key only for a service with exactly one fault, which no ecommerce service has. Real grounding is `_ground_set_flags_against_flagd`, which needs `automation.fault.clear` (demo layer only), so grounding **fails open** in CI and evals. Phase 5.
- **Metrics pending their fields:** blast-radius accuracy (Phase 5); memory influence + wrong-memory influence (Phase 3). Reported as `not_measurable_yet`, never as zero. Evidence grounding, hypothesis discrimination and timeline accuracy now have fields to score against (`EvidenceMatrix`, the ranked `matrices`, `IncidentTimelineView`) but the scorers themselves are still unwritten — a Phase 7 task, not a blocked one.
- **Truth files cover only the 12 application faults.** `demo/ecommerce/failure_injection/infrastructure_layer/` holds ~10 more (dns_failure, pool_exhaustion, memory_exhaust, packet_loss, disk_full, network_latency, service_timeout, cpu_spike, dependency_failure) with **no truth files**, so they are unevaluated — while V6 already claims to diagnose three of them.
- **No token accounting.** Needs gateway instrumentation; latency is measured, tokens are not.

---

## Phase 2 — what landed

### Created

| File | Role |
|---|---|
| `investigation/facts.py` | typed telemetry facts over the existing `evidence.Backend`; the `Availability` split that decides whether absence is evidence |
| `investigation/catalog.py` | 10 **generic SRE failure classes**, evidence-triggered, each returning what argues *against* it |
| `investigation/scoring.py` | additive rule-based scoring with `rule_trace`; `PRIOR_MAX` caps history below every current-evidence term |
| `investigation/pipeline.py` | scope → timeline → baseline → completeness → hypotheses → matrix → rank |
| `tests/test_rca_investigation_pipeline.py` | 35 cases, pure functions over literal facts |
| `tests/test_rca_deterministic_confidence.py` | 12 cases: the model does not own the number |

### Modified

`agent.py` (investigation wired in; `_Observation`; `_authoritative_confidence`;
`_verdict_from_investigation`; grounding check) · `models.py` (`Investigation` aggregate +
optional `investigation` field on the verdict) · `evidence.py` (added
`payment_failures_total`; `CachingBackend`; query constants made public and
single-sourced) · `evals/rca_synthetic.py` · `evals/rca_metrics.py` ·
`tests/test_rca_context_adapter.py`

### The candidate space is failure *classes*, not failure *keys*

Hypotheses come from a generic vocabulary — dependency unreachable, crash loop, CPU
saturation, downstream timeout, OOM kill, memory pressure, latency regression,
application error, change regression, stale alert. No injection key appears anywhere in
the reasoning path, so the catalog generalises to incidents nobody injected (constraints
#9/#10). `action_hint` is a remediation *class*; turning one into a runnable key is
grounding's job in Phase 5.

### Requirement #7 is now met

Confidence is the deterministic score of the top-ranked hypothesis. The model's figure is
recorded as `llm_stated_confidence` and not used — in **either** direction: a pessimistic
model cannot drag a well-supported conclusion down. When the model's prose does not
describe the hypothesis that was scored, the verdict is downgraded to `UNCERTAIN`, because
a number computed for one claim must not sit beside a different one.

### RCA now works with no LLM at all

`_verdict_from_investigation` builds a verdict from the stages alone. Before Phase 2 an
absent model meant "insufficient evidence" however decisive the telemetry was. This is
what makes the accuracy tier reproducible in CI.

### Measurements

| Metric | Phase 1 | Phase 2 (live LLM) | Phase 2 (stub, no LLM) |
|---|---|---|---|
| root-cause accuracy | 12/12 | **11/12** | **11/12** |
| remediation accuracy | 0.917 | **1.0** | 0.0 *(by design — see below)* |
| false-positive rate | 0.0 | 0.0 | 0.0 |
| abstention rate | 0.0 | 0.083 | 0.083 |
| under-confidence | 0.0 *(metric was blind)* | 0.083 | 0.083 |
| HITL safety | 1.0 | 1.0 | 1.0 |
| evidence coverage | 0.876 | **0.927** | 0.927 |
| Brier score | 0.0099 | 0.0824 | 0.0824 |
| mean latency | 8.4 s | 7.7 s | ~0 s |

**Read the accuracy row carefully.** It went *down* by one, and that is not a regression:
`order_service_payment_timeout` is a genuine tie between "the downstream timed out" and
"the order path is slow" — two readings of the same failure — and the scorer now reports
`UNCERTAIN` instead of asserting one. An honest abstention counts as a miss under
keyword grading. Brier rose for the same reason: the deterministic scores are less extreme
than the model's were, which is what calibration looks like when a system stops claiming
0.95 on single-signal evidence.

`remediation_accuracy 0.0` on the no-LLM path is deliberate: the deterministic verdict
proposes **manual** steps only, because grounding a remediation class against the action
registry is Phase 5. It is not a failure to find the fix; it is a refusal to invent an
executable one.

### Findings from Phase 2

Four bugs found by the evaluation and fixed, all of the same family — **treating
"rules out a rival" as "argues against this"**:

1. A REACHABLE gauge scored as *contradicting* a store outage. Two healthy stores beside
   one dead one is the strongest localisation available; it cost the correct answer 0.35 on
   the scenarios where gauges were most decisive.
2. Healthy gauges scored as contradicting a *timeout* hypothesis, for the same reason.
3. "No db-flavoured error counter is moving" scored as contradicting a store outage — but
   error counters are per-service and incomplete (`user-service` publishes
   `login_failure_total`, which RCA does not query), so the silence meant "we have no such
   counter", not "the database is fine". It took `user_service_mysql_down` from PROBABLE to
   UNCERTAIN.
4. The CPU rule was the only one that did not record the rivals it ruled out, so it always
   looked single-source and drew the lone-signal penalty. Two *correct* CPU diagnoses were
   withheld as UNCERTAIN at 0.45.

Plus: `dependency_timeout` fired on latency alone, making it indistinguishable from
`latency_regression`; the grounding check dropped 3-character tokens and split pod names on
whitespace, so "CPU-saturated" never matched `resource_saturation_cpu`; and mixed
naive/aware timestamps crashed the timeline sort (caught by its own test).

**Disclosure on method:** rules 4 and the grounding fix were made *after* seeing eval
results. Both are defensible on principle — every other rule already recorded its
exclusions, and dropping `cpu` for being short was plainly a bug — but the sequence is
worth stating, because iterating rules against a 12-scenario set risks fitting them to it.
The generic-failure-class design limits the damage (nothing keys on a scenario), and the
real test is unseen incidents, which this set cannot provide.

### Metric flaw found and fixed

`underconfidence_rate` read 0.0 while three correct diagnoses were being withheld:
abstentions have `root_cause_correct=False` by design, so measuring underconfidence off
that field alone could not see over-caution. It now counts an abstention whose prose named
the right cause. A metric that cannot see over-caution will reward it.

## Phase 2 — test results

Full suite, `uv run pytest -q -m "not integration and not llm"`:

```
2323 passed · 1 skipped · 2 xfailed · 0 failed · 0 errors   (exit 0)
```

2276 (Phase 1) + 47 new Phase 2 tests = 2323, so every new test is accounted for and no
pre-existing test regressed. The 1 skip and 2 xfails are the same pre-existing, unrelated
ones. `ruff check` and `ruff format --check` clean across 393 files.

> **Reading the raw log:** the run writes progress to stdout and the summary line to
> stderr, and the capture separates the two (done in Phase 1 so HF/ServiceNow stderr noise
> could not pollute captured JSON). So `scratchpad/suite_*.txt` ends at `[100%]` with no
> summary — the counts above come from tallying the progress characters, which also
> confirms 0 `F` and 0 `E`. If you want the summary line in the file, merge the streams.

---

## Phase 3 — what landed

### Created

| File | Role |
|---|---|
| `agents/rca_agent/investigation/memory.py` | the provider allowlist, recall, prior weighting (reliability + decay), the promotion lifecycle, and influence reporting |
| `aiops/tools/incident_history/providers/outcomes.py` | `rca_outcomes` provider — searches **verified RCA outcomes**, never the truth-file corpus |
| `tests/test_rca_memory.py` | 60 cases: lifecycle, decay, reliability shrinkage, prior ceiling, attachment, recall mechanics |
| `tests/test_rca_memory_blindness.py` | 30 cases: the allowlist, verified-only store, symptom-only queries, and the agent path staying cold |

### Modified

`aiops/state/models.py` (`RCAOutcomeRow`) · `aiops/state/repository.py` (outcome CRUD, primitives only) ·
`aiops/state/__init__.py` (additive migrator entry) · `incident_history/base.py`
(`recorded_hypothesis_class`) · `incident_history/retriever.py` (registers the provider) ·
`investigation/models.py` (`selected_hypothesis_class`, `HistoricalInfluence.changed_ranking`) ·
`investigation/pipeline.py` · `investigation/scoring.py` (docstring precision) · `agent.py`
(`_memory_signatures`, recall wired into `_investigate`) · `evals/rca_eval.py` ·
`evals/rca_metrics.py`

**The prompt was not touched.** Priors do not enter `SYSTEM_PROMPT_V6` or the user prompt —
that is Phase 4. This keeps the byte-identity gate in `test_rca_context_adapter.py` green by
construction, and `TestPromptUnchanged` pins it.

### The plan changed on contact: an allowlist, not a chain

Phase 2's checkpoint said to register the outcome provider "through the existing chain so
`AIOPS_INCIDENT_HISTORY_PROVIDERS` gives cold-start vs learning for free". **That plan
leaks.** `corpus.py` maps each truth file's `root_cause` onto `recorded_cause`, the chain's
default provider is `mock`, and the twelve ecommerce truth files are the evaluation's graded
answer key. Recalling through the chain would have handed RCA the string it was about to be
scored on — and accuracy would have gone *up*, which is what makes it dangerous rather than
merely wrong.

So RCA gets `OUTCOME_BACKED_PROVIDERS = {"rca_outcomes"}` — an allowlist. Any other
configured provider is **refused and reported**. Three independent guards, each with its own
tests: the allowlist; a store that emits only `verified`/`trusted` rows regardless of what the
caller asks for; and symptom-only queries (`_memory_signatures` carries alert names, metric
names and reason codes — never a cause, never the agent's own hypotheses).

`AIOPS_RCA_MEMORY_PROVIDERS` is the switch: unset → `rca_outcomes`, explicitly empty → cold
start. The platform chain is untouched, because the restriction is a fact about RCA rather
than about the corpus — other consumers may legitimately search the truth files.

### Three defects found and fixed

1. **`PRIOR_MAX` did not enforce what its docstring claimed.** "History may never make the
   difference between abstaining and asserting" — but 0.10 is larger than the gap between
   status thresholds (0.30/0.50/0.75) and larger than `DISCRIMINATION_MARGIN` can absorb, so
   an evidence-only 0.45 plus a full prior crossed into `PROBABLE`, and a pair the evidence
   could not separate became separated. Fixed in `_status_for`: **the status band and the
   discrimination test are both computed from the prior-free score.** A prior may raise the
   number inside its band and may reorder candidates; it may not upgrade the claim.
2. **The join key was per-incident, so memory did nothing at all.**
   `Hypothesis.hypothesis_id` is `digest(incident_id, rule_id)` — unique per incident. Keying
   recall on it meant priors were retrieved, attenuated, reported as eligible, and then
   attached to *no* hypothesis. Memory looked correctly wired and was inert. The join is now
   on `Hypothesis.category` (the catalog rule id), and the carrier field was renamed
   `recorded_hypothesis_class` so the confusion is harder to repeat. **Found by the
   evaluation, not by the tests** — every unit test constructed its own priors and passed.
3. **`UNPROVEN_RELIABILITY` was unreachable.** `success_rate` is a raw ratio, so one verified
   occurrence scored 1.0 — identical to fifteen-of-seventeen. A pattern carried maximum prior
   weight the first time it was ever confirmed, which is exactly the over-trust the constant
   existed to prevent. Fixed with `RELIABILITY_SMOOTHING = 2.0`: 1-of-1 earns 0.67, 15-of-17
   earns 0.84, never-confirmed stays a hard 0.0.

Plus one correctness fix in the provider: a same-service match with **no shared symptom** was
clearing the similarity floor on the service term alone, so every past incident on a service
became a prior for every new one — "this service has had incidents before", dressed up as
precedent.

### Measurements — `AIOPS_LLM_PROVIDER=stub`, no LLM, simulated telemetry

| Metric | cold-start | learning | poisoned-memory |
|---|---|---|---|
| root-cause accuracy | 0.9167 | 0.9167 | **0.9167** |
| Brier score | 0.0824 | 0.0824 | 0.0804 |
| false-positive rate | 0.0 | 0.0 | 0.0 |
| memory consulted | 0.0 | 1.0 | 1.0 |
| priors changed the top hypothesis | — | **0.0** | **0.0** |
| **wrong-memory influence** | — | **0.0** | **0.0** |
| current evidence cancelled a prior | — | 0.25 | 0.33 |

**The poisoned arm is the result worth having.** Every scenario was seeded with a
deliberately wrong, *verified*, same-symptom precedent, and accuracy did not move: 0.9167 in
all three arms, `wrong_memory_influence_rate` 0.0. In a third of scenarios the
contradiction-cancels-prior rule actually fired. This arm uses no truth data at all, which
is what makes it the cleanest measurement in the set.

**It is not vacuous.** Priors genuinely attached — 2-4 eligible per scenario, and in the
poisoned arm three reached the *top* hypothesis (level `weak`, and the Brier shift is those
three). Before the join-key fix this same table read 0.0 for the right-looking wrong reason.

**Learning showed no accuracy gain, and that is reported as a null result.** Nothing in this
scenario set is a tie, because the simulated telemetry is decisive by construction (Phase 1
caveat #4: the simulator emits UNREACHABLE for exactly the failing store). A bounded
tie-breaker has nothing to break. Memory should be expected to help where evidence is
ambiguous, and this corpus has almost no ambiguity — so **do not read the 0.0 as "memory
does not work", and do not read it as "memory works" either.** The mechanism is exercised by
`test_changed_ranking_is_measured_by_ranking_twice`, which constructs a ranking flip
directly.

### Memory influence is now measured, not estimated

Ranking is pure, so `investigate` ranks twice — with priors and without — and reports
`HistoricalInfluence.changed_ranking`. That moved `historical_memory_influence` and
`wrong_memory_influence_rate` out of `PENDING_METRICS`, and splits influence into
`helpful_memory_influence_rate` vs `wrong_memory_influence_rate`, because an average gain
that hides one stale precedent dragging a scenario to a confident wrong answer is not an
improvement.

### What Phase 3 deliberately does not do

- **Nothing on the analysis path writes memory.** Recording happens after verification, which
  is Phase 6. Pinned by `test_an_outcome_is_not_recorded_merely_by_analysing`.
- **A human correction can only de-weight the wrong class, never boost the right one.** The
  outcome is filed under the hypothesis the agent *selected*, and a correction supplies prose,
  not a class. Capturing a corrected class is HITL work — Phase 6.
- **`run()` stays zero-I/O.** A recall is a database read, and `_investigate` returns early
  when `offline=True`.

## Phase 3 — test results

Full suite, `uv run pytest -q -m "not integration and not llm"`:

```
2414 passed · 1 skipped · 2 xfailed · 0 failed · 0 errors   (exit 0)
```

2323 (Phase 2) + 91 new Phase 3 tests = 2414. `ruff check` and `ruff format --check` clean
across 397 files.

**The first run of this suite failed, and the failure is worth recording** because it was a
test asserting on mutable global state. `test_every_registered_provider_is_either_allowed_or_deliberately_not`
read the live `retriever._PROVIDERS` dict, and `tests/test_incident_history.py` registers
fakes named `boom` and `badhealth` that are never removed — so the ratchet reported two test
doubles as unclassified shipped providers. It passed in isolation and failed in the full suite
depending on ordering, the same state-bleed class as `#151`.

It was also asserting the wrong thing. A runtime-registered provider is not something this
repo ships, and RCA's allowlist refuses it whatever it is called. `retriever.BUILTIN_PROVIDERS`
now snapshots the shipped set at import, the ratchet reads that, and
`test_a_runtime_registered_provider_cannot_feed_rca_memory` proves the narrower scope gives up
no protection. Verified by running the polluting file *first* in one process.

**Lesson for later phases:** any governance check over `_PROVIDERS`, the tool registry, or
another module-global registry must read a snapshot taken at import, never the live object.
---

## Phase 4 — what landed

### Created

| File | Role |
|---|---|
| `tests/test_rca_prompt_v7.py` | 64 cases — what V7 must not contain, what it must keep, the runtime vocabulary, the investigation block |

### Modified

`prompts.py` (`SYSTEM_PROMPT_V7`, `INVESTIGATION_BLOCK`, `ACTION_VOCABULARY_BLOCK`,
`NO_ACTIONS_BLOCK`, `RCA_PROMPT_USER_V2`) · `agent.py` (`_action_vocabulary`,
`_render_action_block`, `_render_investigation_block`, V7 wired in) ·
`remediation_map.py` (demoted to a documented fallback) · `evals/rca_metrics.py`
(`action_precision`) · `tests/test_rca_memory_blindness.py` (`TestPromptUnchanged`
replaced, not deleted)

### Q2, delivered: what came out

| Removed | Why it is injection truth |
|---|---|
| The mechanism taxonomy (all three) | `INJECT_LATENCY_SECONDS`, `INJECT_CPU_LOAD`, `INJECT_HTTP_500`, `INJECT_MEMORY_LEAK`, `INJECT_DELAY_SECONDS`, `MYSQL_HOST unresolvable`, "scaled to zero", "overwrites /etc/resolv.conf", "holds ~200MB resident" |
| The 60-line DISAMBIGUATION table | A hand-written answer sheet for the twelve graded scenarios: *"EcommerceRedisDown alone does NOT establish that Redis is down … the cause is DNS on payment-service"* |
| All 15 failure keys | Including the two worked examples in the OUTPUT schema |

V7 is **8,847 chars vs V6's 12,361 — 28% shorter.**

**My own import-time leak check was too narrow, and a test caught it.** I asserted three
sample keys; `order_service.http_500` survived inside the JSON schema's `flag` field
description — the one place a key appears as an aside rather than as part of the list. The
import guard now checks all three service *prefixes*, and
`tests/test_rca_prompt_v7.py` parametrises over every key rather than a sample. A
narrow assertion on a leak is worse than none, because it reads as coverage.

### What went in

The table can go because the platform no longer needs the model to diagnose. V7 receives
the ranked investigation — classes, scores, and the supporting / contradicting /
checked-absent / gap evidence — and is asked to **explain** it, and to state disagreement
openly (the one thing here the platform cannot compute for itself).

The executable vocabulary moved to the **user** message, resolved per request by
`_action_vocabulary`: the action registry first (`_live_flag_names`, which asks
`automation.fault.clear` what it accepts), `remediation_map` only as a labelled offline
fallback. So a fault added to the platform reaches the model with no prompt edit, and a
removed one disappears. It is also **service-scoped** now — 4 keys for payment-service
rather than all 15 — so a cross-service key cannot be proposed.

### Measurements — live Anthropic, same simulated telemetry, V6 vs V7

| Metric | V6 before | V7 after | Δ |
|---|---|---|---|
| root-cause accuracy | 0.9167 | 0.9167 | 0 |
| service accuracy | 1.0 | 1.0 | 0 |
| remediation accuracy (recall) | 1.0 | 1.0 | 0 |
| **action precision** | 0.923 | 0.923 | 0 |
| false-positive rate | 0.0 | 0.0 | 0 |
| abstention rate | 0.0833 | 0.0833 | 0 |
| HITL safety | 1.0 | 1.0 | 0 |
| Brier | 0.0824 | 0.0824 | 0 |
| mean latency | 7.8 s | **10.0 s** | +28% |

**The headline: removing the injection truth cost nothing measurable.** Every accuracy and
safety metric is identical. Remediation held at 1.0 with the keys supplied only at runtime,
which is the specific thing that was at risk — the model picks a runnable action from a
list it has never seen in training or in a prompt constant.

**Read the root-cause row with care.** It was *predicted* to be uninformative and it is:
the Phase 3 stub arm (no model at all) also scores 0.9167, so the deterministic pipeline
carries this metric and V7 feeding it the ranked class lets the model echo it. The prompt's
own contribution is visible in remediation accuracy, action precision, and the
false-positive rate — not here.

**Latency rose 28%** because the investigation block lengthens the prompt. One LLM call
still, as required. Worth watching if a token budget lands (Phase 7).

### A regression the existing metric could not see

Two scenarios changed which action keys they proposed:

- `order_service_payment_timeout`: 2 keys → 1. An **improvement** from service scoping —
  the cross-service `payment_service.gateway_timeout` is gone.
- `user_service_crashloop`: 1 key → 2. It now also proposes `user_service.mysql_down` on a
  crashloop, which is wrong.

Net precision is unchanged at 0.923 — both prompts propose exactly one surplus key — but
the *identity* moved from a defensible sibling (the gateway timeout is registered under
both services for the same fault) to an unrelated one. `remediation_accuracy` asks only
whether the expected key is present, so it reported a flat 1.0 through both. **`action_precision`
was added in response**: every proposed `set_flag` renders as an approve button, so a
surplus key is a button that clears a fault the incident does not have.

### Phase 4 — test results

Full suite, `uv run pytest -q -m "not integration and not llm"`:

```
2480 passed · 1 skipped · 2 xfailed · 0 failed · 0 errors   (exit 0)
```

2414 (Phase 3) + 64 new V7 cases + 2 net from replacing the Phase 3 prompt prohibition =
2480. `ruff check` and `ruff format --check` clean.

Green on the first attempt this time, and the run passed 83% — where the Phase 3 attempt
broke — so the `BUILTIN_PROVIDERS` ratchet fix holds under real suite ordering rather than
only in the isolated repro.

### `TestPromptUnchanged` was replaced, not deleted

Phase 3 asserted priors reached the prompt in no form. Phase 4 renders the investigation,
and §27 requires the operator to be *told* when history moved a conclusion — so the flat
prohibition became four narrower assertions: the system prompt names no memory concept;
memory appears in the user message only when it contributed; it is labelled as precedent,
never as evidence from this incident; and no memory id reaches the model.

---

## Phase 5 — what landed

### Created

| File | Role |
|---|---|
| `investigation/impact.py` | blast radius — who is affected, and who was never looked at |
| `investigation/recovery.py` | failure class → runnable action, risk assessment, verification plan |
| `tests/test_rca_recovery.py` | 47 cases across grounding, impact, matching, risk, verification, wiring |

### Modified

`agent.py` (`_ensure_executable_action` rewritten, `_steps_from_recovery_options`,
`executor_available`, vocabulary threaded into `_investigate`) ·
`investigation/pipeline.py` (three new stages) · `investigation/models.py` (additive
`blast_radius`, `recovery_options`, `verification` on `Investigation`) ·
`remediation_map.py` (`flag_for_service` documented as caller-less) ·
`evals/rca_metrics.py` (`blast_radius_accuracy` reason corrected)

### The grounding hole, and a second one found while fixing it

`_ground_set_flags_against_flagd` asked `_live_flag_names()`, which reaches a provider
registered in the **demo layer only**. Offline — CI, every eval run, any laptop without
the cluster — it returned `None` and the function **returned the steps unchanged**. It
failed open on exactly the paths where nothing else was checking, so an invented action
key reached the verdict as a one-click apply.

The second hole was worse because it applied *online* too: the check compared against the
**global** key set, so `order_service.http_500` proposed for `payment-service` passed
validation. An action that runs and fixes a different service's problem is worse than one
that fails.

Both are closed by sharing one authority with the prompt: `_action_vocabulary(service)`
resolves the registry first and the static map second, service-scoped. The list the model
is *offered* and the list it is *held to* are now the same list by construction, pinned by
`test_grounding_and_the_prompt_share_one_authority`. It still fails open in one case only —
no registry **and** no static entry — and records that it did.

`flag_for_service` was deleted as a call site: it returned a key only for a service with
exactly one fault, which no ecommerce service has, so the branch that *corrected* a wrong
key was unreachable. Not repaired — choosing a remediation from a service name is the
lookup this agent exists to replace.

### Two bugs I wrote and caught in the same phase

1. **`executable` was true offline.** I computed it as `"registry" in source` — and the
   fallback string reads "the action **registry** was unreachable". Substring-matching
   prose turned a negation into a confirmation and collapsed the whole
   grounded/executable distinction. Now compared against a frozenset of source constants,
   pinned by `test_the_check_is_not_a_substring_match`.
2. **The same action was proposed twice.** Two hypotheses matched one key —
   `dependency_unavailable` and `application_error` are two readings of one Redis failure —
   producing two identical approve buttons. The higher-ranked hypothesis now claims the
   action and the rival becomes a manual step that says why.

### A plan premise that was wrong

The Phase 4 checkpoint said Phase 5 would replace "the single `BlastRadius` enum on the
verdict". **`RCAVerdict` has no such field** — `blast_radius` lives on `RankedFixStep`, and
it means *action risk*, not incident spread. Those are different questions: a tiny fix to a
widely-spread outage is low action-risk and high incident-spread. So the report is carried
structurally on `Investigation.blast_radius`, the step value comes from the risk
assessment, and the `derive_blast_radius` helper I had written was **deleted** rather than
left as new dead code in the same phase that removed the old dead code.

### Measurements — stub (no LLM), simulated telemetry

| Metric | Phase 3 stub | Phase 5 stub |
|---|---|---|
| root-cause accuracy | 0.9167 | 0.9167 |
| **remediation accuracy** | **0.0** *(manual-only by design)* | **0.75** |
| action precision | — | 0.9 |
| false-positive rate | 0.0 | 0.0 |
| HITL safety | 1.0 | 1.0 |

**That 0.0 → 0.75 is the phase.** It was 0.0 by construction because grounding could not
run without the cluster, so the deterministic path had to emit manual-only steps. With
grounding working offline the no-LLM path now proposes the correct executable key on 9 of
12 scenarios — with no model involved at all.

Not 1.0, and the shortfall is honest: `match_action_key` refuses ambiguous matches. Where
two keys tie on the same tokens it proposes nothing rather than guessing, which is the
intended behaviour and shows up as a miss under a recall metric.

### `blast_radius_accuracy` cannot be delivered, and why

The report now exists and is populated, so the *field* is no longer the blocker. But **no
truth file records an expected impact set** — no `affected_services`, no expected
`ImpactState`. There is nothing to score against. The `PENDING_METRICS` entry was
rewritten to say so: this is truth-file authoring work, not scorer work. Claiming the
metric because the field landed would have been the exact flattery this evaluation exists
to prevent.

### Phase 5 — test results

First run failed — genuinely, not a harness artefact — and the failure was a real
regression: `tests/test_rca_remediation.py::test_ensure_executable_action_downgrades_invented_flag_for_unmapped_service`,
a **pre-existing** test. Service-scoping the vocabulary had collapsed two different
answers into one: "the registry answered and this service has no action" (authoritative;
an invented key must be downgraded) and "nobody could tell us what is runnable"
(ignorance; failing open is correct) both read as "empty vocabulary → skip grounding". So
`frontendFailure` on an unmapped service survived as a clickable button — the exact defect
Phase 5 exists to close, reintroduced by the fix for it.

Fixed by distinguishing `VOCAB_UNAVAILABLE` (fail open) from every other empty-vocabulary
source (downgrade). A second, related fix followed from the same test file: a **dotless**
legacy handle (`emailMemoryLeak`) names no service at all, so a mismatch cannot be proven
the way a dotted cross-service key can — rejecting one the registry lists would invent a
fault. Both are now pinned in `tests/test_rca_recovery.py`. Confirmed in the definitive combined
run — see `## Full suite — Phases 5-7` below: **2604 passed, 0 failed.**

**Lesson for later phases:** a "fails open on empty" check needs to ask *why* the answer
was empty before deciding whether empty means safe.

---

## Phase 6 — what landed

### Created

| File | Role |
|---|---|
| `agents/rca_agent/learning.py` | the only writer of RCA outcome memory from the live path: `record_verified_outcome`, `apply_human_correction`, `invalidate_outcome` |
| `tests/test_rca_learning.py` | 37 cases — recording, recurrence-based trust, corrections, and the AST-checked boundary |

### Modified

`agents/resolution_verifier/verifier.py` (`_record_rca_outcome`, called from both the
PASS and FAIL branches of `Verifier.verify`)

### The loop closes at the only honest point

`record_verified_outcome` is called from the **verifier's** verdict, never from `analyze`.
That ordering is the whole design: Phase 3 built a store whose entries may only influence
a ranking once recovery was confirmed, and a store with no verified writer stays empty
forever. The call is fire-and-forget and lazily imported, matching the precedent already
in that method (the PASS branch reaches into `knowledge_synthesizer.snow_watcher` the same
way) — recording is bookkeeping and must never affect a closure.

**A FAIL is recorded too**, and stays `UNVERIFIED` — recallable by nothing, but written,
because an approved, executed prediction that did not work is the most informative record
the system produces. Discarding it would leave calibration measuring only the successes.

### Trust is earned, not asserted

`TRUST_THRESHOLD` verified recurrences of one `(service, failure class)` pair promote a
row from `VERIFIED` to `TRUSTED`. Counted by querying the store rather than tracked
incrementally, so the count cannot drift from what was actually recorded. A human
correction counts as a **rejection** for the class that was refuted — it teaches that the
predicted class was wrong here, so the pattern's track record must get worse, not better.

`apply_human_correction` and `invalidate_outcome` exist, are tested, and have **no
production caller**. That is the honest state: a correction needs a human and there is no
approval-screen UI yet. Wiring one is UI work, not Phase 6 work.

### The learning boundary is a control, not a promise

The constraint — learning must never modify RCA source, prompts, remediation logic, tool
registrations, or safety rules, automatically, on any number of incidents — is asserted
against `learning.py`'s own **AST**: no reference to `SYSTEM_PROMPT_V6`/`V7`, no
`write_text`/`open`, no `register_provider`/`register_tool`, no `DEFAULT_LEVELS`, no
`exec`/`eval`/`setattr`, and no import of a prompts or policy module.

**The first version of that test scanned raw text and failed on its own docstring** — the
word "prompts" appears in the paragraph *explaining* the rule. A substring scan cannot
tell a rule from a mention of it. Rewritten to walk the module's AST (imports, attribute
accesses, call targets), the same discipline `tests/test_layering.py` already uses for the
`aiops`/`agents` boundary.

### Phase 6 — test results

37 new tests (`tests/test_rca_learning.py`). Included in the definitive combined run
below — 2604 passed, 0 failed.

---

## Phase 7 — what landed

### Created

| File | Role |
|---|---|
| `tests/test_rca_eval_metrics_phase7.py` | 37 cases — category scoring, evidence grounding, fabrication detection, discrimination margin, timeline coverage |

### Modified

`evals/rca_metrics.py` (`CATEGORY_ALIASES`, `CATEGORY_SUBTYPES`, `normalise_category`,
`category_satisfies`, `_grounding_check`, five new `ScenarioScore` fields, five new
`MatrixReport` properties, `category_mismatches`, module docstring)

### Category accuracy: the metric that was sitting unused since Phase 1

Every truth file carries `grading.must_identify_category` — a direct label untouched by
phrasing. `root_cause_accuracy` matches keywords against free-text prose and accepts any
one synonym, which the Phase 1 checkpoint already called an **upper bound**, not an
estimate. Phase 7 finally reads the direct label, so the upper bound can be checked
against something. On the deterministic path they agree almost exactly — 0.9167 both —
with the one divergence explained correctly as an abstention, not a wrong classification.

### The alias table was wrong on its first attempt, and the mistake is worth keeping visible

I assumed `Hypothesis.category` was the catalog's `rule_id` (Phase 3's own join-key
comment says exactly this) and built `CATEGORY_ALIASES` to map truth-file names onto
`rule_id`s. Only 4 of 10 catalog rules have a `category` identical to their `rule_id` — the
other six differ (`oom_kill` → category `resource_exhaustion_memory_oom`,
`process_crash_loop` → `startup_failure`, etc.) — but the truth files were authored
against the **category** vocabulary directly, so most already matched with no table at
all and the wrong premise still produced plausible-looking numbers. Corrected: the table
now aliases truth-file spelling onto `category`, is three entries, and a new
`CATEGORY_SUBTYPES` table (kept separate, and directional) lets a more specific answer
satisfy a general label without letting a vague one satisfy a precise one.

**Found by re-deriving the numbers from first principles, not by a test** — every unit
test had constructed its own literal categories and passed regardless of which vocabulary
the table pointed at. `tests/test_rca_eval_metrics_phase7.py::TestCategoryVocabularyMatchesNatively`
now pins the correct claim (checked against real truth files and the real catalog) instead
of an invented proxy for it.

### Evidence grounding and fabrication, scored independently of the agent's own check

`_grounding_check` does not call `agent._grounded_in_investigation` — a grader that shared
the implementation it grades could not catch a bug in it. It asks two separate questions:
does the prose restate a token from the selected hypothesis's evidence (`evidence_grounded`,
`None` when there was no investigation to check against), and does it cite a metric-shaped
identifier that appears in *no* evidence statement at all (`fabricated_citations`) — the
system prompt's own words for "the single worst failure mode", scored here rather than
trusted.

On the deterministic path: `evidence_grounding` 1.0, `fabricated_citation_rate` 0.0.

### What is now measured, versus what remains blocked

`PENDING_METRICS` went from four entries to two, and the two that remain are blocked for a
different reason than "unwritten": `timeline_accuracy` and `blast_radius_accuracy` both
have the field they need; neither has a truth file recording the expected sequence or
impact. `mean_discrimination_margin` and `timeline_coverage` (a count, explicitly not an
accuracy) are new and reported unconditionally.

### Phase 7 — test results

37 new tests (`tests/test_rca_eval_metrics_phase7.py`). Included in the definitive
combined run below — 2604 passed, 0 failed.

---

## Constraints any further change will trip

(Named generically rather than "Phase 8": Phase 0-7 is the complete locked scope — see
the resolution below. These constraints apply to any future work on this agent, phased
or not.)

- `tests/test_retrieval_call_sites.py` — a ratchet on capability call-site counts per file. Fails in **both** directions. Adding the `payment_failures_total` query changes no count (same capability string, already counted), but any *new* capability must be added to `RETRIEVAL_LEDGER` in the same commit.
- `tests/test_rca_context_adapter.py` — gates byte-identical prompt output between the adapter and the legacy path. Add new sections *around* the existing observation block, never woven into it.
- `tests/test_layering.py` — `aiops/` may never import `agents/` (AST-checked).
- CI: `ruff check` + `ruff format --check` + `pytest` + `evals.harness --ci --min-pass-rate 0.85` + `opa`. `AIOPS_LLM_PROVIDER=stub`, 60s per-test cap, no cluster, no real LLM. `--locked` uv sync: a dependency change needs a committed `uv.lock`.
- `RCAVerdict` is `extra="forbid"`; the five v0 fields are a public contract (dashboard `api.ts`, eval grammar, `RCAResultRow`). Additive optional fields only.
- HITL levels live in `aiops/policy/gate.py::DEFAULT_LEVELS` (runtime authority) **and** `policies/hitl.rego` (reference). Nothing forces agreement — edit both.
- mypy reports 42 pre-existing errors elsewhere in the tree; **0** in `agents/rca_agent`. Keep it that way. mypy is not in CI.
- `tests/test_rca_memory_blindness.py::test_every_registered_provider_is_either_allowed_or_deliberately_not` — a ratchet on the history registry. Registering a new provider fails the suite until it is classified as outcome-backed or corpus-backed. Deliberate: an unclassified provider is how a leak arrives unnoticed.
- `tests/test_rca_memory_blindness.py::TestHowMemoryMayAppearInThePrompt` — pins *how* memory may reach the model (user message only, only when it contributed, labelled as precedent, no ids). Replaced the Phase 3 flat prohibition in Phase 4; relax it the same way if a later phase needs to, never by deletion.
- `tests/test_rca_prompt_v7.py` — a two-sided ratchet on the prompt. No injection mechanism, no failure key, no alert→key mapping may appear in `SYSTEM_PROMPT_V7`; the evidence rules and untrusted-input guard must remain. It carries a positive control asserting V6 *did* contain all of it, so the guard cannot pass by guarding nothing.
- `prompts.py` asserts at **import** that V7 leaks nothing — a half-applied edit fails the interpreter rather than shipping. Add to `_leak` when a new injection detail appears anywhere in the tree.
- `aiops/state/_migrate_add_columns_if_missing` — the `rca_outcomes` table is new, so `create_all` covers a fresh DB, but a dev DB created mid-Phase-3 needs the additive `selected_hypothesis_class` entry already in `_NEEDED`. Extend that list for any further column.
- `tests/test_rca_learning.py`'s `TestLearningBoundary` — an **AST**, not text, check that `agents/rca_agent/learning.py` references no prompt symbol, no file write, no tool/policy registration, no dynamic execution. Checked this way because a text scan fails on the module's own docstring explaining the rule (it contains the word "prompts"). A new forbidden name belongs in the test's `@pytest.mark.parametrize` list, never as a substring check.
- `evals/rca_metrics.CATEGORY_ALIASES` / `CATEGORY_SUBTYPES` — kept intentionally tiny (`test_the_alias_table_is_small` asserts ≤3 entries). A category mismatch should usually mean the catalog's `Hypothesis.category` genuinely needs a new class, not a new alias; growing this table to make a number agree is the thing `tests/test_rca_eval_metrics_phase7.py`'s history section exists to warn against.

## Full suite — Phases 5-7

Run after Phase 5, 6 and 7 code together (Phase 5's first attempt caught a real
regression, fixed above; the count and exit code below are the actual result of the
re-run, not asserted ahead of it):

```
2604 passed · 1 skipped · 2 xfailed · 0 failed · 0 errors   (exit 0)
```

2567 (Phases 5+6, confirmed in their own full-suite run) + 37 new Phase 7 tests = 2604
exactly. `ruff check` and `ruff format --check` clean. This is the definitive result —
Phases 5, 6 and 7 together, including the Phase 5 regression fix.

## Regenerating the golden

`agents/rca_agent/evals/golden.json` is **generated**, not hand-written — every input
passes through `rca_input_from_truth`, so blindness is structural.
`tests/test_rca_eval_blindness.py::test_golden_inputs_have_not_drifted_from_the_adapter`
fails if the file and the adapter disagree. To regenerate, build the 12 cases from
`discover_ecommerce_truth_files()` via `rca_input_from_truth`, with
`expected = {affected_service, root_cause_status: "insufficient_evidence",
max_confidence_score: 0.3}`.

## All locked phases (0-7) are closed

- [x] A valid baseline exists and is reproducible
- [x] Data contracts landed and tested
- [x] Blindness enforced on the production path, with a positive control
- [x] The wrong fallback is gone; `run()` is genuinely offline
- [x] Stage modules: scope, timeline, baseline, completeness, hypotheses, evidence matrix, deterministic scoring
- [x] `payment_failures_total` query added (the redis_down finding) — coverage 0.876 → 0.927
- [x] Requirement #7: confidence is platform-derived, both directions, with the model's figure recorded separately
- [x] RCA produces a verdict with no LLM at all
- [x] `RCAOutcomeRow` + repository CRUD; only `verified`/`trusted` rows are recallable
- [x] `rca_outcomes` provider registered; RCA restricted to it by **allowlist**, not by chain order
- [x] `EvidenceMatrix.priors` populated from real recalls, weighted by reliability and freshness
- [x] `HistoricalInfluence` filled, including `changed_ranking` measured by ranking twice
- [x] Current evidence outranks memory *arithmetically* — status band and discrimination are prior-free
- [x] Poisoned-memory arm: a wrong verified precedent moved nothing (0.0 wrong-memory influence)
- [x] **Q2 delivered:** no injection mechanism, alert→key table, or failure key remains in the prompt; the vocabulary is resolved per request from the action registry
- [x] Before/after eval on the live LLM (V6→V7): every accuracy and safety metric unchanged
- [x] The model's role is now to explain the ranked investigation, not to diagnose from raw telemetry
- [x] Action grounding works **offline** — shares one authority with the prompt vocabulary, closing the fail-open hole `_ground_set_flags_against_flagd` had in CI/evals/no-cluster
- [x] `BlastRadiusReport`, `RecoveryOption` / `RiskAssessment` (tri-state) produced as stages
- [x] Deterministic (no-LLM) `remediation_accuracy` is no longer 0.0-by-design — grounding runs offline, so it proposes real executable keys (0.0 → 0.75 on the stub arm)
- [x] Outcome recording wired to the **verifier's** verdict (both PASS and FAIL), never to `analyze`
- [x] Trust earned by recurrence, counted from the store; corrections counted as rejections
- [x] The learning boundary (never modifies code/prompts/tools/safety rules) is an AST-checked control, not a docstring
- [x] `category_accuracy` reads the truth files' direct label and agrees with `root_cause_accuracy` on the deterministic path
- [x] Evidence grounding and fabricated-citation rate scored independently of the agent's own grounding check
- [x] `PENDING_METRICS` reduced to the two genuinely truth-blocked entries (`timeline_accuracy`, `blast_radius_accuracy`) — both have their field, neither has ground truth
- [x] **Full suite across Phases 5-7 together — 2604 passed, 0 failed, exit 0.** See `## Full suite — Phases 5-7` above.

## Two decisions closed out after Phase 7

**`scenario_id` — kept, documented as demo-only.** Checked what actually depends on it
before deciding, rather than removing it on a hunch. It is *not* redundant with the Phase 2
deterministic
pipeline: `_fallback_verdict` already checks the investigation first and only reaches the
`scenario_id` branch when there is genuinely no evidence to reason from (zero telemetry —
the rehearsed demo calling RCA before Prometheus has scraped, or a fully offline/stub run).
Two tests exercise exactly that zero-evidence case
(`test_explicit_scenario_id_still_reaches_the_locked_verdict`,
`test_locked_scenario_fallback_annotates_set_flag_action`), and `incident_commander` /
`knowledge_synthesizer`'s test fixtures depend on the same path. Removing it would
destabilise the rehearsed demo — an explicit POC deliverable — for no safety gain, because
the boundary that actually matters is structural and already in place:
`rca_input_from_truth` never passes `scenario_id`, enforced by 69 cases in
`tests/test_rca_eval_blindness.py`. No accuracy figure in this repo passed through this
branch. Strengthened the comment at `agent.py`'s `_LOCKED_SCENARIO` to state this argument
in full so it doesn't need re-deriving.

**Phase 8 — none.** 0-7 is the complete locked scope. The original spec's sketch through 8
was superseded by the Phase 0 approval, which enumerated exactly seven phases and no more.
Q3 (ServiceNow auto-close) stays parked as future scope, per the owner's own decision — it
is not a phase this work owes, and inventing one to fill a number would be scope creep
against the owner's explicit "scope creep is the silent killer" guidance (CLAUDE.md POC
scope discipline). If a rollout/docs/ops phase is wanted later, it is a new decision to
make when it's actually needed, not a gap in what shipped.

## What is genuinely still open (not blocking completion)

- **`order_service_payment_timeout`** still ties two hypotheses describing the same
  failure from different angles. Memory did not break the tie (it is the one scenario
  where a prior touched the top hypothesis and the ranking still did not change). Either a
  tie-break rule (prefer the hypothesis naming the *callee*) or accepting the abstention is
  defensible; worth a decision rather than a silent tune.
- **A human correction cannot boost the correct class**, only de-weight the refuted one —
  the correction is prose and no field records the corrected *class*. Capturing it needs
  an approval-screen UI, which does not exist yet; `learning.apply_human_correction` is
  ready for it.
- **The ~10 infrastructure-layer faults** (`demo/ecommerce/failure_injection/infrastructure_layer/`)
  have no truth files and are unevaluated, though V6/V7 already claim to diagnose three of
  them.
- **No token accounting.** Latency is measured throughout; tokens need gateway
  instrumentation.
- **The original spec sketched through Phase 8**; the locked order the owner approved in
  Phase 0 runs 0-7. Whether a distinct Phase 8 (rollout, docs, the parked ServiceNow
  auto-close expansion) was intended is still an open question for the owner, not a
  decision made here.
