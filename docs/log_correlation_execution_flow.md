# Log Correlation Agent (RA-007) — Logging & Execution Flow

How one incident moves through `correlate()`, what each stage owns, and what it
emits. Written against the eleven-stage pipeline on
`feature/log_correlation_improve`.

> **Read this first.** The pipeline is almost entirely *silent* on the happy path.
> A full synthetic run emits **five** log records — and four of them come from a
> single stage (11, the graph walk) reporting per-node resolutions. Nine of the
> eleven stages say nothing at all on success. Everything a responder needs is in
> `CorrelationResult.audit_metadata.decision_trace`, not in the log stream. The
> consequences are spelled out in [Observability gaps](#observability-gaps) — that
> section is the most useful part of this document.

---

## Entry points

| Caller | Function | Input |
|---|---|---|
`POST /api/correlate` ([server.py:884](../demo/ui/server.py)) | `correlate(payload)` | `CorrelationInput` built from `{service, window_minutes\|start\|end, triage_verdict?, classification?, topology?}` |
RA-008 Incident Commander ([agent.py:322](../agents/incident_commander/agent.py)) | `correlate(payload)` | service + window from the alert, plus RA-001 verdict and RA-002 classification |
Eval harness | `run(dict) -> dict` | Forces `force_synthetic=True` so the golden gate is deterministic |
CLI | `python -m agents.log_correlation --fixture <id>` | Golden fixture, trailing window |

```python
CorrelationInput(
    service="checkout",                      # required
    window=TimeWindow(start=..., end=...),   # required, end >= start
    triage_verdict={...} | None,             # RA-001 output, as a dict
    classification={...} | None,             # RA-002 output, as a dict
    topology={"checkout": ["payment"]} | None,
)
```

`_resolve_topology()` is **not** a public entry point — it is stage 1, called only
from inside `correlate()`.

---

## Stage-by-stage

Ordering note: evidence (stage 6) is built **before** confidence (stage 7) so the
confidence breakdown can cite `evidence_id`s. Safe because scoring never reads
evidence — it only borrows ids for the explanation.

### 1 · Topology resolution

| | |
|---|---|
**Owner** | `agent._resolve_topology` → `aiops.tools.topology.resolve` |
**Logger** | `aiops.tools.topology.resolver` |
**Levels** | `DEBUG` on success · `WARNING` on provider failure, unknown provider name, budget exhaustion |
**ToolResult.metadata** | Providers going through the registry return `{"provider": "mock", "matched": true}`; the resolver does not itself write `ToolResult` |

**decision_trace** — the full set, not a sample. Exactly one is emitted per call:

| Line | When |
|---|---|
| `topology: N downstream dep(s) from supplied map` | `payload.topology` was given (chain not consulted) |
| `topology: N downstream dep(s) from <provider>` | a tier resolved |
| `topology: 0 downstream dep(s) from <provider>` | tier answered with a record listing no dependencies |
| `topology: <provider> returned no dependencies` | tier answered with nothing at all |
| `topology: lookup error (<err>); no topology` | any tier FAILED (reported ahead of `attempts[0]`) |
| `topology: <note>; no topology` | highest-priority tier UNAVAILABLE — e.g. `itsm.cmdb.dependencies not registered`, `cmdb circuit open`, `unknown provider 'cmdbb'` |
| `topology: resolution budget exhausted; no topology` | total budget blown mid-walk |
| `topology: no provider in the resolution chain; no topology` | chain parsed to nothing |

**Which tier a line refers to.** A FAILED tier anywhere wins, because an error is
the most actionable outcome of the walk and must not be hidden by a higher-priority
tier that merely wasn't configured. Otherwise the line describes `attempts[0]`, the
highest-priority tier consulted — which is why unknown names are recorded in their
configured priority slot rather than dropped, and why every UNAVAILABLE note names
its own tier.

**On "byte-identical" (a claim this document used to make, wrongly).** Four lines
carry over verbatim from the pre-chain single `itsm.cmdb.dependencies` lookup — the
counted line, the 0-dep line, `cmdb returned no dependencies`, and
`itsm.cmdb.dependencies not registered; no topology`. The rest are **new**: the
pre-chain code had no circuit breaker, no provider chain and no budget, so it could
not emit `cmdb circuit open`, `unknown provider`, `budget exhausted`, or the
no-provider line. `lookup error (<err>)` also differs in substance — the legacy line
interpolated `type(exc).__name__`, whereas the registry now yields free-text
`"TypeName: message"`. Grep on the `topology:` prefix, not on whole lines.

These are operator-facing via RA-008 and the console, so rewording an existing one
silently breaks anyone matching it.

### 2 · Fan-out fetch (parallel)

| | |
|---|---|
**Owner** | `_fetch_logs` / `_fetch_traces` / `_fetch_metrics` in a `ThreadPoolExecutor` |
**Logger** | `agents.log_correlation.agent` — but **nothing is logged here at all** |
**decision_trace** | `logs: 112 matching line(s) from loki` · `logs: loki error (<err>)` · `logs: capability observability.logs.query not registered` |
**ToolResult.metadata** | Loki returns `{"provider": "loki", "url": ...}`; the breaker returns `ok=False, error="HTTPError: circuit open (Loki unreachable)"` |

Each fetcher returns `(signals, reachable)`. `reachable` distinguishes "asked, got
nothing" from "could not ask" — a distinction that decides whether stage 3 fires.

### 3 · Synthetic fallback

| | |
|---|---|
**Owner** | `_synthesize_signals` |
**Fires when** | `live_signals` is empty from *all three* sources, or `force_synthetic=True` |
**decision_trace** | `synthetic path forced (deterministic eval)` · `synthetic: generated 3 deterministic signal(s) for 'checkout' (suspects=['payment'])` |
**Provenance** | Sets `audit_metadata.signal_source = "synthetic"` so a verdict is never mistaken for live data |

### 4 · Rule-based correlation

| | |
|---|---|
**Owner** | `_rank_signatures`, `_suspects_from_topology`, first-error selection, spike detection |
**Logger** | none |
**decision_trace** | `first error: logs @ <ts> — <sig>` · `top signatures: [...]` · `cross-source recurrence detected` · `error-rate spike: 3 error-severity signal(s) in window` · `suspect component(s): ['payment']` |

> ⚠️ `first error:` is emitted even when nothing is error-severity — the fallback
> selects `timeline[0]`. Tracked as a `strict=True` xfail.

### 5 · Summary

| | |
|---|---|
**Owner** | `_generate_summary` → `aiops.llm.complete` |
**Logger** | `agents.log_correlation.agent` — `WARNING` only, on LLM failure: `LLM summary failed: %s` |
**decision_trace** | `assembled evidence summary` |
**Failure mode** | Falls back to `_template_summary`; the trace line is identical either way, so **the trace does not record whether the LLM was used** |

### 6 · Structured evidence

| | |
|---|---|
**Owner** | `evidence_builder.build_evidence` |
**Logger** | `WARNING` on failure only: `structured evidence build failed: %s` |
**decision_trace** | `evidence: 3 structured object(s)` · or `evidence: build failed (<Type>); omitted` |
**IDs** | `evidence_id` and `correlation_id` are SHA-256 **identity** hashes — deterministic, so re-running one incident yields identical ids. `evidence_id` covers only `(correlation_id, source, signature)`; severity, occurrences and timestamps are **not** hashed, so an id repeating does not mean the underlying data is unchanged |

### 7 · Confidence scoring

| | |
|---|---|
**Owner** | `_confidence_breakdown` → `confidence.explain_confidence` |
**Logger** | none |
**decision_trace** | `confidence 0.9 from 4 applied rule(s), 1 unapplied` (+ `, capped`) |
**Note** | `_confidence()` delegates here, so the returned score and the explained score cannot diverge |

### 8 · Incident timeline

| | |
|---|---|
**Owner** | `_build_incident_timeline` → `timeline_sources` + `timeline.build_timeline` |
**Logger** | `agents.log_correlation.timeline_sources` — `DEBUG` on kube-client absence, `WARNING` on event-fetch failure |
**decision_trace** | `timeline: 4 entr(ies) from 4 source(s) [...]` · plus a second line carrying `coverage_note` when incomplete · `timeline: no evidence to build from; omitted` (stage 6 produced nothing — the timeline is keyed off evidence) · `timeline: build failed (<Type>); omitted` |

### 9 · Historical retrieval *(opt-in)*

| | |
|---|---|
**Owner** | `aiops.tools.incident_history.search_similar`, guarded by the resilience middleware |
**Logger** | `aiops.tools.resilience` (`WARNING` on timeout/exception) · `aiops.tools.incident_history.retriever` (`WARNING` on unknown provider) |
**decision_trace** | `history: N similar incident(s) via <provider>` · or `history: retrieval failed (<Type>); omitted` |
**Gate** | `AIOPS_INCIDENT_HISTORY` — `None` when off, which is **not** the same as an empty match list |

### 10 · Deployment / configuration context *(opt-in)*

| | |
|---|---|
**Owner** | `aiops.tools.change_context.collect_change_context`, guarded |
**Logger** | `aiops.tools.change_context.collector` (`WARNING` on provider raise) · `...providers` (`DEBUG` on git/kube unavailability) |
**decision_trace** | `change context: N record(s) from ['github', 'feature_flags']; unavailable=['kubernetes']` |
**Gate** | `AIOPS_CHANGE_CONTEXT` |

### 11 · Multi-hop dependency graph

| | |
|---|---|
**Owner** | `correlate` inline → `aiops.tools.topology.graph_builder.build_resolved_graph` |
**Logger** | `aiops.tools.topology.graph_builder` — `WARNING` on node-cap truncation and on a resolver raise mid-walk · `aiops.tools.topology.resolver` — `DEBUG` per node resolved |
**decision_trace** | `graph: 9 node(s), 9 edge(s), depth 2 via cmdb` · `graph: walk found no edges from 'checkout' (leaf service — a tier holds a record listing no dependencies)` · `graph: no topology tier answered for 'checkout'; dependencies unknown, not absent` · `graph: build failed (<Type>); omitted` |
**Caps** | `AIOPS_TOPOLOGY_GRAPH_MAX_DEPTH` (default 3) · `AIOPS_TOPOLOGY_GRAPH_MAX_NODES` (default 50) |
**Gate** | none — unlike stages 9 and 10 this always runs |

Separate from the one-hop suspect list: this is topology as resolved, unfiltered by
suspicion, so a consumer can see the whole blast radius rather than only the
services the evidence implicated.

**Zero edges is three different facts, and the field keeps them apart.** An earlier
version dropped an edgeless walk to `None`, which made a leaf service
indistinguishable from a walk that never ran:

| `dependency_graph` | Meaning |
|---|---|
| `None` | no result produced — the stage was skipped or it raised |
| present, `edges == []`, `root_answered=True` | a tier answered; this service genuinely has no downstream dependencies (a leaf) |
| present, `edges == []`, `root_answered=False` | no tier could answer; dependencies are **unknown**, not absent — `coverage_note` says so |

The third row is why `root_answered` exists. Collapsing it into the second would
render a total resolution failure as a positive "nothing depends on this", which is
worse than the ambiguous `None` it replaced. `provider` cannot carry the
distinction: it is populated from `winning_provider`, which is unset for a genuine
leaf too.

Whether an edgeless graph is worth *drawing* is a rendering decision — the console
falls back to the suspect list and names which of the three cases it is showing. A
leaf service is never drawn as a graph of itself.

**This is the only stage that logs on success, and it is the loudest thing in the
pipeline.** The walk is BFS with one resolution per node, and the resolver emits a
`DEBUG` line per resolved node, so a 9-node graph on the default `cmdb,mock` chain
produces 4–5 records where the other ten stages together produce one. Resolutions
are cached, so a re-walked subtree is cheap, but the log volume scales with graph
size, not with incident complexity.

Note the sibling function `build_service_graph` is **not** what runs here. That one
fetches the entire edge set from the OTel tier in a single query (cheap, but needs a
source that returns all edges, and OTel is off in the default chain) and carries a
gRPC-only coverage note. Stage 11 uses `build_resolved_graph`, which works with the
per-service tiers by walking them. Per-service tiers can say what X calls but never
what calls X, hence `reverse_known=False`: an empty `upstream` here means
*unobservable*, not absent, and `coverage_note` says so.

---

## Topology tier selection — how the chain reports itself

The newest and most failure-prone path, so in detail. `resolver.resolve()` walks
the chain and records **every** attempt in `TopologyResolution.attempts`, not just
the winner.

| Situation | Log | Level |
|---|---|---|
Tier resolved | `topology: 'cmdb' resolved 5 dep(s) for 'checkout' in 0.0ms` | `DEBUG` |
Tier failed | `topology: provider 'otel' failed for 'checkout' (<err>); breaker open 30s` | `WARNING` |
Provider raised | `topology: provider 'snow' raised for 'checkout': <exc>` | `WARNING` |
Unknown name in env | `topology: unknown provider 'bogus' in AIOPS_TOPOLOGY_PROVIDERS; skipping` | `WARNING` |
Budget exhausted | `topology: budget 3.0s exhausted for 'checkout' after 1 attempt(s)` | `WARNING` |

These lines are emitted **once per resolved service, not once per correlation.**
Stage 1 resolves the incident service; stage 11 then walks the chain and resolves
every node it reaches. So the `resolved N dep(s)` line repeats — see
[gap 4](#4--stage-1-and-stage-11-are-indistinguishable-in-the-log-stream).

Skips are **not logged** — a tier skipped for an open breaker, a failed health
check, or a cache hit produces no log line. It appears only in `attempts[]` on the
returned object:

```python
res.attempts  # [TopologyResult(provider='otel', status=EMPTY, ...),
              #  TopologyResult(provider='cmdb', status=RESOLVED, cached=False, ...)]
res.winning_provider  # 'cmdb'
```

**So the log stream shows which tier *won*, but not which were skipped or why.**
To see the full chain you need the returned object or the `decision_trace` line,
which names only the winner.

The four-way status is what makes an empty answer readable:

| Status | Meaning | Falls through | Trips breaker |
|---|---|---|---|
`RESOLVED` | found dependencies | no | no |
`EMPTY` | asked, genuinely nothing | yes | **no** |
`UNAVAILABLE` | could not ask (unconfigured / breaker open) | yes | no |
`FAILED` | asked and errored | yes | **yes**, after retries |

---

## Structured vs free text

**Everything is free text.** There is no structured logging anywhere in this agent
or its four seams — every call is `logger.warning("topology: provider %r failed for %r (%s)", ...)`
style `%s` interpolation producing a prose line. There is no JSON formatter, no
`extra={}` field dict, no log context.

Machine-readable output exists only in the **return objects**:

| Structured | Where |
|---|---|
`decision_trace: list[str]` | `audit_metadata` — semi-structured; stable prefixes (`topology:`, `logs:`, `evidence:`) make it greppable, but values are interpolated prose |
`TopologyResolution.attempts` | per-tier status, latency, cached flag |
`ChangeContext.sources_unavailable` | named unavailable sources |
`resilience.stats()` | per-provider counters — `calls`, `retries`, `timeouts`, `starved`, `breaks`, `breaker_skips`, `exceptions`, `cache_hits`. **`starved` is the one to watch under load**: it counts calls that never reached their provider because no worker slot came free, so a rising count means the shared pool is the bottleneck rather than any backend — the lever is `AIOPS_RESILIENCE_WORKERS`, not the provider's timeout |
`ToolResult.metadata` | `{"provider": ..., "matched": ...}` per registry call |

---

## Observability gaps

Four findings that came out of writing this document. All are real, none are fixed.

### 1 · The correlation id never reaches a log line

`correlation_id` is generated in `evidence_builder` at **stage 6** and lives only
on `Evidence` and `IncidentTimeline` objects. No logger call includes it.

**You cannot grep a single incident's logs end to end.** There is no incident id,
request id, or trace id in any log record. With two concurrent correlations the log
lines interleave with nothing to separate them — a `WARNING` about a failing Loki
cannot be attributed to either incident.

The fix is a `contextvars`-based log filter injecting `correlation_id` into every
record, with the id moved earlier (it derives from `service` + `window`, both
available at stage 0, so it can be computed on entry rather than at stage 6).

### 2 · The happy path is silent, and what noise there is comes from one stage

Stages 2, 4 and 7 log nothing at any level. Stages 3, 5, 6, 8, 9 and 10 log only on
failure. Stage 1 emits one `DEBUG`. Stage 11 emits one `DEBUG` per node walked — so
a synthetic checkout run totals five records, four of them from stage 11.

That distribution is the problem, not the count. **There is no log evidence that a
correlation ran, when it started, or how long it took** — only that a graph walk
touched some services. Turning stage 11's caps down to quiet the log stream would
leave the run with a single line; turning them up buys more of the least
incident-specific information in the pipeline. Neither knob gets you a run record.

The narrative lives entirely in `decision_trace`, which is only visible if you have
the result object — so post-hoc debugging from logs alone is not possible.

### 3 · Success and fallback are indistinguishable in two places

- Stage 5 appends `assembled evidence summary` whether the LLM answered or the
  template did. The `WARNING` on LLM failure is the only signal, and it is easy to
  miss beside an unchanged trace line.
- Stage 1 emits the same `topology: N downstream dep(s) from cmdb` whether the
  answer came from a live query or a cache hit. `TopologyResult.cached` records it;
  the trace does not.

### 4 · Stage 1 and stage 11 are indistinguishable in the log stream

Both resolve topology through `aiops.tools.topology.resolver`, so both produce the
same `topology: 'cmdb' resolved N dep(s) for 'X' in 0.0ms` line at the same level
from the same logger. Nothing marks which stage asked.

The incident service appears **twice** — once for stage 1, once as the root of the
stage 11 walk — with byte-identical text on the synthetic checkout run:

```
DEBUG aiops.tools.topology.resolver  topology: 'cmdb' resolved 5 dep(s) for 'checkout' in 0.0ms
DEBUG aiops.tools.topology.resolver  topology: 'cmdb' resolved 5 dep(s) for 'checkout' in 0.0ms
DEBUG aiops.tools.topology.resolver  topology: 'cmdb' resolved 1 dep(s) for 'cart' in 0.0ms
```

Reading the stream, a duplicate line is not obviously two stages rather than a retry
loop or a cache that failed to hold. Combined with gap 1 (no correlation id), two
concurrent correlations make this unreadable: four identical lines for the same
service, attributable to neither incident nor stage.

Both fixes are small — pass a stage or purpose label into the resolver's log call,
and note that dependency lists resolved during a walk are expected to repeat the
root. Neither is done.

Also visible above: services whose resolution comes back `EMPTY` are not logged at
all. `checkout` reports 5 dependencies but only `cart`, `payment` and `shipping`
produce lines; `currency` and `email` are leaves and vanish. So the log stream
under-reports the walk, while `ServiceGraph.nodes` has all 9.

---

## Reproducing the trace

```bash
uv run python -m scripts.trace_correlation_demo
```

Runs the checkout/payment scenario on the synthetic path with all loggers at
`DEBUG` and per-stage timings, by wrapping the agent's real functions — so the
numbers describe the actual call graph rather than a parallel re-implementation.

Two limits worth knowing before you trust its output:

- **The timing table covers stages 1–8 only.** Stages 9, 10 and 11 are not wrapped,
  so they contribute to `TOTAL correlate()` but appear nowhere in the breakdown —
  they land in the `unaccounted` remainder. On a synthetic run that remainder is
  ~1.7ms of ~4.3ms, and stage 11's walk is inside it. Do not read the table as a
  full latency profile.
- **The printed `RESULT` block omits `dependency_graph`.** Evidence, confidence
  derivation and the incident timeline are each rendered; the graph is visible only
  as its one-line `decision_trace` entry. To inspect it, hold the result object.
