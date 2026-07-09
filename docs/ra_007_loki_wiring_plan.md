# RA-007 — Wire Log Correlation to a real Loki backend + dashboard page

**Issue:** [#220](https://github.com/UbiquotousPanda/AIops/issues/220) · **Related:** original Log Correlation issue #36
**Agent:** RA-007 Log Correlation (Reactive-Active) · **HITL:** None (read-only)

## Goal

Turn RA-007's **logs** leg from a permanent `KeyError → synthetic fallback` into
a real Loki-backed signal, and give the agent a dashboard surface. Today the
agent does trace + metric correlation plus a deterministic synthetic generator;
`observability.logs.query` has **no registered provider**, so a live run logs
`logs: capability observability.logs.query not registered` and confidence caps
lower because logs never contribute.

**The agent and policy layers need zero changes.** `_fetch_logs` already parses
Loki's native `streams`/`values` wire shape, and `observability.logs.query`
already maps to `level=none` in both `policies/hitl.rego` and
`aiops/policy/gate.py`. The work is a **provider + infra + frontend** stack on
top of an agent that is already waiting for it.

## Non-goals

- No change to the agent's correlation rules, models, or HITL wiring.
- No change to the eval-harness path: `run()` keeps `force_synthetic=True` so
  the golden gate stays deterministic regardless of cluster reachability.
- No OpenSearch. (The current agent docs claim an OpenSearch/Loki dual provider
  and an `AIOPS_LOGS_PROVIDER` switch that were never implemented — those docs
  are corrected to describe the single real Loki provider.)

## Delivery: two PRs

- **PR1 — Loki provider (backend, cluster-independent).** Self-contained and
  CI-safe: the provider, its registration, docs, unit tests, conftest wiring,
  `.env.example`, and the agent-doc de-fiction. Merges without a cluster.
- **PR2 — Make it live (infra + frontend + verification).** Loki Helm deploy,
  collector logs routing, port-forwards, the `/api/correlate` endpoint, the
  `LogCorrelation` page, and the live smoke test.

---

## Track A — Loki provider (PR1)

**New: `aiops/tools/observability/loki.py`** — mirrors `jaeger.py`:

- Env: `AIOPS_LOKI_URL` (default `http://localhost:3100`), `AIOPS_LOKI_TIMEOUT`
  (`10`), `AIOPS_LOKI_CONNECT_TIMEOUT` (`2`), `AIOPS_LOKI_CIRCUIT_OPEN_SECONDS`
  (`30`).
- Process-local **circuit breaker** copied from `jaeger.py` (`_circuit_open_until`,
  `_reset_circuit_for_tests()`, the guard in `_get`). Load-bearing: RA-007 fans
  out logs/traces/metrics in a `ThreadPoolExecutor`; an unreachable Loki must
  fail fast, not add connect-timeout latency to every correlate call.
- One `@tool`: `name="loki.observability.logs.query"`,
  `capability="observability.logs.query"`, `provider="loki"`.
- Query `GET /loki/api/v1/query_range` with `query={service_name="<svc>"}`,
  `start`/`end` (convert the agent's ISO-8601 to unix-nanoseconds — Loki's
  required form), `limit`, `direction=backward`.
- **Mapping** (the one real transform): Loki returns `data.result`; map it to
  `data.streams` so `_fetch_logs` finds it, and promote each stream's
  `detected_level` label to `level` (that's what `_fetch_logs` reads).

**Register: `aiops/tools/observability/__init__.py`** — add `loki` to the import
+ `__all__` + docstring capability list.

**Docs: `aiops/tools/README.md`** — flip the `observability.logs.query` row from
`(Phase 1)` to `live: Loki (loki.observability.logs.query)`.

**Test: `tests/test_loki_circuit_breaker.py`** — clone the six Jaeger
breaker tests + one mapping test (`result→streams`, `detected_level→level`). All
mocked; no cluster.

**conftest: `tests/conftest.py`** — add `AIOPS_LOKI_URL=http://127.0.0.1:1`
(+ sub-second timeouts) `setdefault`s next to Prometheus/Jaeger, import `loki`,
and add it to the autouse circuit-reset fixture.

**`.env.example`** — add the `AIOPS_LOKI_*` block next to Jaeger.

**Doc de-fiction (PR1):** correct `agents/log_correlation/agent.py` docstring and
`agents/log_correlation/README.md` to describe the single Loki provider (drop the
OpenSearch / `AIOPS_LOGS_PROVIDER` / `opensearch-grafana-datasource.yaml` claims
that never existed).

---

## Track B — Infra: deploy Loki + ship logs (PR2)

1. **Free memory** in `demo/otel-demo/values.yaml`: disable `image-provider` and
   `fraud-detection` under `components:` (~600 Mi; node is ~94% committed).
   Verify exact key names via `helm show values open-telemetry/opentelemetry-demo`.
   Tradeoff: storefront images 404 (cosmetic; no failure scenario touches them).
2. **Deploy Loki** via `helm upgrade --install loki grafana/loki` into the
   `otel-demo` namespace in `infra/bootstrap.ps1` (single-binary, filesystem
   storage, `auth_enabled: false`). Release/service must resolve to `loki:3100`.
3. **Route logs** to Loki in `values.yaml` under the `opentelemetry-collector:`
   sub-chart: add an `otlphttp` exporter `endpoint: http://loki:3100/otlp` and
   add it to `service.pipelines.logs.exporters`.
4. **Port-forward** `loki` 3100 in **both** `infra/port-forward.ps1` and
   `start.ps1` (they duplicate the list).
5. **Grafana datasource** ConfigMap `infra/loki-grafana-datasource.yaml`
   (sidecar-labeled) so Loki shows in Explore — the manual-verification tool.
6. **Verification gate (blocking before UI is trusted):** inject a scenario,
   confirm in Grafana Explore that `{service_name="product-catalog"}` returns
   lines and the label value matches the agent's lowercased query.

---

## Track C — Frontend (PR2)

- **`POST /api/correlate` in `demo/ui/server.py`** — mirror `/api/rca`: a
  `CorrelateRequest` (`service`, `window`, optional `triage_verdict` /
  `classification` / `topology`), `await asyncio.to_thread(correlate, ...)`,
  return `.model_dump(mode="json")`. No `force_synthetic` — production attempts
  live. Default the window to the last ~15 min for one-click demos.
- **Types `src/types/api.ts`** — `CorrelatedSignal`, `EvidenceProvenance`,
  `CorrelationResult` mirroring `agents/log_correlation/models.py`.
- **Client `src/lib/api.ts`** — `correlate(...)` next to `rca`.
- **Page `src/pages/LogCorrelation.tsx`** (+ optional `CorrelationView.tsx`),
  modeled on `RcaConsole.tsx`/`RcaView.tsx`: timeline color-coded by source, top
  signatures, suspected components, confidence meter, decision-trace `<details>`,
  and a **`live | synthetic` provenance badge** from `signal_source`.
- **Route + nav** — `App.tsx` route + `Sidebar.tsx` `ITEMS`/surface lists.

---

## Track D — Verification / "Done when" (PR2)

1. `uv run python -m agents.log_correlation --fixture slow_product_catalog`
   decision trace shows `logs: N matching line(s) from loki` (N>0) and
   `signal_source: "live"`.
2. Dashboard renders a live RA-007 evidence pack end-to-end.
3. `tests/test_loki_live_smoke.py` exists and **skips when `AIOPS_LOKI_URL` is
   unreachable** so CI documents the real path without flaking.

---

## Risks & gotchas (ranked)

1. **`service_name` label mismatch** — most likely failure. Loki's OTLP
   conversion and the agent's lowercased query must agree. Mitigated by the
   Grafana Explore gate (B6).
2. **Loki service DNS** — collector's `http://loki:3100/otlp` needs the release
   to produce a service literally named `loki` in `otel-demo`.
3. **Timestamp format** — the agent passes ISO-8601; Loki `query_range` wants
   RFC3339-nano / unix-ns. The provider converts.
4. **Memory** — if trimming two components isn't enough, Loki stays Pending;
   may need to lower Loki's own requests.
5. **Chart keys** — `components.*` and `opentelemetry-collector.config.*` must be
   verified against the pinned chart version, not assumed.

## Sequencing

A + C are cluster-independent and can proceed first (unit tests + synthetic page
stay green). B is the gating item for live verification. D flips C from synthetic
to live with no code change.
