# Onboarding two devs who cannot install WSL — findings + options

**Author:** Gaurav Patil · **Date:** 2026-08-07
**Problem:** two new devs have Windows 11 / 16 GB / Python / good internet, but **WSL is a hard no** (IT policy). `ONBOARDING.md` §2.1 opens with `wsl --install`. What can they actually do, and what are our options?

**Bottom line:** WSL is not a dependency of *this project*. It is a dependency of *Rancher Desktop*, which is the only reason we need it. **775 of 777 tests, the whole agent chain, the eval harness, all four SPAs and the FastAPI server run with no cluster and no kubeconfig at all** — verified on this machine, results in §2. So the two devs can be productive in ~30 minutes today; the only question is how (and whether) we give them a live cluster on top.

---

## 1. How WSL is actually used

### Every WSL reference in the repo

`grep -ri wsl` across the whole tree returns **7 files, zero of them code**:

| File | What it says |
|---|---|
| `ONBOARDING.md` §2.1, §2.3, §3 | `wsl --install --no-distribution`, `wsl --list --verbose`, and the "relocate the Rancher Desktop distros to D:" procedure |
| `ONBOARDING.md` §11, §"ImagePullBackOff" | "git-bash / WSL" as one way to `tail -f`; "the WSL VHDX has grown and C: is full" |
| `RUNNING.md` (end of day) | "cluster state is preserved in the WSL VHDX" |
| `SESSION_NOTES.md` | "WSL2 + Rancher Desktop (k3s) running" — a status note |
| `docs/demo_readiness_audit{,_v2}.md` | "Audit host: Windows 11, Rancher Desktop k3s (WSL2)" — provenance |
| `demo/hitl-ui/package-lock.json` | false positive (a substring in a package hash) |

**No `.ps1`, `.py`, `.sh`, or `.yaml` in the repo invokes `wsl.exe`.** Nothing imports it, nothing shells into it, no path is hardcoded to a `\\wsl$\` share.

### So where does the requirement come from

One place: **Rancher Desktop on Windows runs its k3s node inside two WSL2 distros** (`rancher-desktop` = containerd + k3s, `rancher-desktop-data` = the data volume). WSL2 is Rancher Desktop's VM backend on Windows; there is no non-WSL mode. That is the entire coupling:

```
WSL2  →  Rancher Desktop  →  k3s  →  the otel-demo namespace
                                     ├── frontend-proxy :8080  (webstore, Grafana, Jaeger, flagd UI)
                                     ├── prometheus :9090
                                     ├── jaeger :16686
                                     ├── loki :3100
                                     └── flagd-config ConfigMap  ← failure injection
```

`CLAUDE.md` explains why we landed on Rancher Desktop and not kind/k3d/Docker Desktop: **org policy bans Docker on dev machines.** Rancher Desktop was the WSL-based way around that ban. For a dev who cannot have WSL either, that reasoning has to be re-opened — which is what §3 does.

### What the cluster provides, capability by capability

This is the precise list of what the two devs lose if they have no cluster:

| Registry capability / feature | Backed by | Behaviour with no cluster |
|---|---|---|
| `feature_flags.set_variant` / `get_variant` / `list_variants` / `reset_all` | k8s API → `flagd-config` ConfigMap ([`aiops/tools/feature_flags/adapter.py`](aiops/tools/feature_flags/adapter.py)) | `/api/scenarios/{id}/inject` and `/reset-all` → **502**; `inject.py` errors. `GET /api/scenarios` still 200s, reporting every scenario as `off` |
| `observability.metrics.query` / `.alerts` | Prometheus :9090 port-forward | `prometheus_reachable: false`; Alert Stream / Active alerts / Severity mix empty; the live-alert sweep and auto-triage loop find nothing |
| `observability.traces.search` / `.services`, `/api/topology` | Jaeger :16686 | Service Graph page empty |
| `observability.logs.query` | Loki :3100 | RA-007 Log Correlation falls back to its synthetic path (circuit breaker opens fast, so it degrades rather than hangs) |
| `observability.metrics.render_panel` | Grafana via :8080 | RA-003's Grafana panel attachment on the ServiceNow ticket is skipped |
| the webstore itself, live traffic, k6 load | frontend-proxy :8080 | no clickable demo app, no organically-generated telemetry |

Everything else in the stack is **cluster-independent and native-Windows**: `uv` + Python 3.12, the four Vite SPAs (`dashboard`, `combined-ui`, `classifier-ui`, `hitl-ui`), the FastAPI server on :8765, SQLite `data/state.db`, ServiceNow PDI (cloud), Azure Foundry Claude / Azure OpenAI (cloud), Slack/Teams/PagerDuty webhooks (cloud), git-crypt secrets.

### Script portability note (matters for every Linux-based option below)

Only `infra/bootstrap.sh` and `infra/teardown.sh` have bash twins. **`start.ps1`, `stop.ps1`, `reset.ps1`, `scripts/demo/fire.ps1`, `scripts/demo/fire-all.ps1`, `scripts/secrets/*.ps1` are PowerShell-only.** Any option that puts the dev loop on Linux needs either bash equivalents or `pwsh` (PowerShell Core) installed in that environment — `pwsh` is the cheaper path, since `Start-Job`, `kubectl`, and `uv` all work there.

---

## 2. What works with no WSL and no cluster — measured, not assumed

Run on this machine with `KUBECONFIG` and `USERPROFILE` pointed at nonexistent paths and `AIOPS_PROMETHEUS_URL` / `_JAEGER_URL` / `_LOKI_URL` pointed at dead ports, i.e. a faithful simulation of a WSL-less laptop:

| Check | Result |
|---|---|
| `demo.ui.server:app` boots | ✅ `/api/health` → 200, **26 capabilities registered**, `prometheus_reachable: false`, `jaeger_reachable: false` |
| `GET /dashboard/` | ✅ 200 |
| `GET /api/fixtures` | ✅ 200 |
| `GET /api/scenarios` | ✅ 200 — degrades cleanly to `current_variant: "off"` per scenario (the `try/except` around `feature_flags.list_variants` swallows the failure by design) |
| `POST /api/triage/fixture/payment_cpu_spike` — the full RA-001→002→003→005+006 chain | ✅ **200.** verdict `Sev-2 / payment / conf 0.75 / Payments Team`, ticket `INC0000001` (mock ITSM), classification, 3 chatops deliveries all `ok:true`, persisted `verdict_id=30 classification_id=184 notification_id=184` |
| `python -m evals.harness` | ✅ **`overall_pass_rate: 1.0`** |
| `pytest -m "not integration and not llm"` | **775 / 777 pass** |

The 2 failures are **pre-existing on `main` and unrelated to the cluster**:
- `tests/test_chatops_seam.py::test_to_record_full_key_contract` — the test's expected key set is missing `runbook`; the record grew a key and the assertion wasn't updated. Fails on a machine *with* a cluster too.
- `tests/test_auto_triage_loop.py::test_loop_survives_live_alerts_failure` — **passes in isolation**, fails in the full run → test-order env pollution, exactly the `demo.ui.server` `load_dotenv()` footgun `CLAUDE.md` warns about.

Also: exactly **one** test file in the entire repo is `@pytest.mark.integration` (`tests/test_loki_live_smoke.py`). The suite was written to be cluster-free.

**Conclusion: roughly 95% of the development surface of this repo needs no WSL.** The gap is narrow and specific: *a live k8s cluster with the OTel demo in it.*

---

## 3. The options

Seven, cheapest-and-fastest first. They are not mutually exclusive — §4 recommends a combination.

### A. Cluster-less dev mode ("Local Lite") — **do this today regardless**

The two devs install nothing but user-scope tooling, run everything native Windows, no cluster.

- **Install:** `winget install --scope user` for `astral-sh.uv`, `Git.Git`, `OpenJS.NodeJS`; GnuPG + git-crypt for secrets. No admin beyond GnuPG (which `SECRETS.md` already flags as needing admin once — a portable GPG or having me hold the keys avoids even that).
- **`.env`:** `AIOPS_LLM_PROVIDER=anthropic` (real Azure Foundry Claude), `AIOPS_USE_MOCK_ITSM=false` (real PDI — it's cloud), Prometheus/Jaeger/Loki/Grafana URLs left unset, `AIOPS_STATE_DB_URL` default.
- **Run:** `uv sync --extra dev --extra ui --extra embeddings`, build the SPAs, `uv run python -m uvicorn demo.ui.server:app --port 8765`.
- **They get:** every agent, the fixture chain end-to-end with real LLM + real ServiceNow + real Slack, all four SPAs, the 777-test suite, the eval harness, truth files, the HITL/approvals flow, policy, state.
- **They don't get:** the inject buttons, Alert Stream, Grafana, Service Graph, the webstore.
- **Time to first commit:** **~30 minutes.** **Cost:** $0. **Risk:** none.

Work this fully unblocks — which is most of what's left: Phase 3 agents (Anomaly Detector, Early Warning, SLO Breach Predictor, Reliability Forecaster — all algorithm + eval work), prompt engineering, eval sets, truth files, dashboard/SPA work, chatops adapters, ITSM, policy/HITL, ADRs, docs, the two failing tests above, and CI.

To make it a first-class mode rather than a workaround (**~half a day**): commit a `.env.nocluster` preset, add a `start-lite.ps1` that skips the cluster probe and the four port-forwards, and add a "no cluster" column to `ONBOARDING.md`.

### B. Share my cluster over a tunnel ("one cluster, three devs")

My laptop keeps the only k3s; the other two reach it over **Tailscale** (free plan covers 3 users / 100 devices) or WireGuard.

- **B1 — read-only observability.** They point `AIOPS_PROMETHEUS_URL` / `_JAEGER_URL` / `_LOKI_URL` / `_GRAFANA_URL` at my Tailscale IP. I bind the port-forwards to `0.0.0.0` instead of `127.0.0.1` (`kubectl port-forward --address 0.0.0.0`, a one-line change in `start.ps1`). They get live alerts, real traces, real Loki logs, Grafana. No cluster mutation. **~1 h to set up. $0.**
- **B2 — full, including inject.** Also share a kubeconfig whose `server:` points at the tunnel. Then `feature_flags.*` works and the inject buttons work. **Caveat: one `flagd-config` ConfigMap = one shared blast radius** — two devs injecting simultaneously overwrite each other. Needs a "who owns the cluster right now" convention (a Slack channel topic is enough).
- **Nothing installs on their machines but Tailscale**, which is the appeal: it doesn't touch the policy that blocked WSL.
- **Risk:** my laptop becomes the team SPOF — must be awake, online, and now serving three people from 16 GB.

### C. Local k8s without WSL: minikube on Hyper-V

```powershell
minikube start --driver=hyperv --memory=6g --cpus=4
.\infra\bootstrap.ps1 -Context minikube      # -Context is already a parameter
.\start.ps1 -Context minikube
```

Fully local, WSL-free, identical dev loop, and `bootstrap.ps1` / `start.ps1` already accept `-Context` so **no code change is needed**. Bonus: minikube ships a real `kubectl`, so the whole `kuberlr` workaround in `CLAUDE.md` and `start.ps1` becomes moot.

- **Requires:** Hyper-V enabled (admin, once) and Windows **Pro / Enterprise / Education** — [Hyper-V is not available on Windows 11 Home](https://minikube.sigs.k8s.io/docs/drivers/hyperv/).
- **The catch that decides this option:** WSL2 and Hyper-V both run on the same Windows Hypervisor Platform. If IT blocked *WSL specifically*, this works. If IT blocked *hardware virtualization / the hypervisor platform*, this dies for the same reason. **Ask IT which it is — see §5.**
- **Time:** ~1 h + ~10 min image pull. **Cost:** $0.
- **Variant C2:** a Hyper-V Ubuntu VM running k3s + the full stack, devs attach with VS Code Remote-SSH. Same prerequisite, more Linux-native, heavier.

### D. Different hypervisor: VirtualBox / VMware VM with k3s

`minikube start --driver=virtualbox`, or a plain Ubuntu VM plus `curl -sfL https://get.k3s.io | sh -` and `infra/bootstrap.sh`.

VirtualBox does **not** use the Windows hypervisor (and actively conflicts with Hyper-V when it's enabled), so this can survive a hypervisor-platform block that kills option C. In many orgs VirtualBox predates the WSL policy and is still allowed.

- **Requires:** VirtualBox install (admin) + VT-x on in BIOS.
- **Reality check:** a 6 GB VirtualBox VM plus VS Code plus a browser on a 16 GB laptop is tight but workable; noticeably slower I/O than WSL2.

### E. GitHub Codespaces + k3d — nothing on the laptop at all

Add `.devcontainer/` with the `docker-in-docker` feature and a `postCreate` that runs `k3d cluster create aiops`, `uv sync`, `npm install`, `infra/bootstrap.sh`. Devs open the repo in a browser tab or attach VS Code Desktop to the Codespace. **Zero endpoint install, zero admin, zero virtualization on their machine** — the cleanest possible answer to "IT won't let me."

This is a well-trodden pattern ([k3d in Codespaces templates](https://github.com/codespaces-lab/kubernetes-in-codespaces)); note that **minikube does not support routable services in Codespaces**, so it has to be k3d or kind.

Honest numbers, because they decide it:

- The OTel demo + Loki needs ~4–5 GB → an **8-core / 16 GB** Codespace.
- Codespaces bills **core-hours** = wall-clock × core count. GitHub Free personal includes **120 core-hours + 15 GB/month** → on an 8-core that is **~15 wall-clock hours/month**. Not enough for two full-time devs.
- Paid: 2-core is **$0.18/hour**, i.e. **$0.09/core-hour**, so 8-core ≈ **$0.72/hour** → ~**$115/dev/month** at 160 h, plus $0.07/GB-month storage. Verify against [GitHub's calculator](https://github.com/pricing/calculator) before committing budget.
- **Cheap hybrid:** run a **2-core Codespace with no cluster** (= option A, in the cloud) at 2 core-h/hour → 120 free core-hours ≈ **60 h/month free**, and get the live cluster from option B or F.
- **Policy check:** the Docker ban is an *endpoint* policy and Codespaces is cloud, so it likely doesn't apply — but the repo lives under a personal org (`UbiquotousPanda`), so Codespaces billing would land on a personal account. Confirm that's acceptable.
- **Work:** `.devcontainer/devcontainer.json` + `postCreate.sh`, plus `start.sh`/`reset.sh` bash twins **or** just install `pwsh` in the image and reuse the existing `.ps1` files. **~1 day.**

Same shape, other vendors if Codespaces is blocked: [DevPod](https://devpod.sh) (open-source, free, brings its own provider), [Coder](https://coder.com) (self-hosted, community tier free), Daytona. Note **Gitpod Classic shut down 15 Oct 2025** and Gitpod/Ona pivoted to agent orchestration — don't build on it.

### F. One shared cloud k3s VM, code stays local — **best value if any budget exists**

Provision **one** Linux VM, `curl -sfL https://get.k3s.io | sh -`, run `infra/bootstrap.sh` once. The OTel demo lives there permanently. All three devs keep coding natively on Windows (the option A stack) and point `AIOPS_*_URL` plus their kubeconfig at it over Tailscale.

This is option B with a server instead of my laptop — and it fixes B's three weaknesses: no SPOF, no 4 GB tax on my machine, cluster up 24/7.

- **Cost for the whole team:** Hetzner CPX41 (8 vCPU / 16 GB) ≈ **€25/month**; DigitalOcean 8 GB ≈ $48; Azure B4ms (4 vCPU / 16 GB) ≈ $120. Cheapest way to give three people the full experience.
- **Isolation:** `bootstrap.sh` takes a namespace, so `otel-demo-dev2` is possible — but 3× the demo won't fit in 16 GB. Realistically one shared demo plus a booking convention, or a bigger VM.
- **Side benefit that matters more than the dev problem:** for the client demo, a cloud cluster reachable from any laptop beats "the demo only runs on Gaurav's ThinkPad." Today the rehearsed demo has a single-machine dependency.
- **Watch:** where does the data live, is a non-corporate cloud VM acceptable for a POC on synthetic data (it should be — `CLAUDE.md` mandates no real customer data), and lock SSH/Tailscale down properly.

### G. Record the cluster, replay it forever ("fixture / replay mode") — **the durable fix**

Capture real Prometheus / Jaeger / Loki / Grafana responses for each of the ~10 scenarios into `demo/fixtures/observability/`, then add a `replay` provider under `aiops/tools/observability/` registered when `AIOPS_OBS_PROVIDER=replay`. Do the same for flagd: an in-memory `feature_flags` provider holding variant state in `data/state.db`.

Result: **the inject buttons work, Alert Stream fills, Log Correlation returns real-looking logs, the Service Graph draws — with no cluster, ever, on any laptop.**

Why this fits the codebase rather than fighting it:
- It is **exactly what the seams exist for.** `CLAUDE.md`'s first non-negotiable is "wrap every external dependency behind a thin internal interface" and every seam already "degrades to a mock/stub when its vars are absent." A replay provider is one more provider behind an existing capability, not a new abstraction.
- It is the **same pattern as `AIOPS_USE_MOCK_ITSM`**, which already does this for ServiceNow — there's precedent and a review-approved shape to copy.
- `demo/truth_files/` already stores ground truth per scenario, and `test_every_scenario_has_a_truth_file` enforces it. Recorded telemetry is the missing half of that pair.

- **Cost:** $0/month. **Effort:** **2–4 days** of engineering + ~1 day capturing on my machine.
- **Payoff well beyond onboarding:** rehearsal-safe demos (no "the cluster died 10 minutes before the client call"), deterministic evals, CI coverage of the live-alert path we currently can't test at all, and every future teammate onboards in 30 minutes on any laptop.
- **Downside:** fixtures drift when scenarios change and someone must re-capture; and replay never proves the real integration works — you still need a real cluster (B/C/F) for that.

### H. Slim the demo profile — an amplifier, not an option

Not a WSL fix, but it makes E and F materially cheaper. `demo/otel-demo/values.yaml` already cuts `opensearch`, `image-provider`, and `fraud-detection`. A `values-minimal.yaml` (frontend + frontend-proxy + payment + product-catalog + collector + prometheus + flagd; drop Grafana, Jaeger, Loki, Kafka, load-generator) should roughly halve the footprint — enough to make a 4-core Codespace viable and a cloud VM a tier cheaper. **~half a day.**

---

## 4. Comparison and recommendation

| | Endpoint install | Admin | IT exception | Live cluster | Setup | Cost/mo | Build effort |
|---|---|---|---|---|---|---|---|
| **A** Local Lite | uv, git, node | no¹ | **none** | ✗ | 30 min | $0 | ~½ day |
| **B1** Tunnel, read-only | Tailscale | no | tunnel app | ✓ read | ~1 h | $0 | ~1 h |
| **B2** Tunnel, full | Tailscale | no | tunnel app | ✓ shared | ~2 h | $0 | ~2 h |
| **C** minikube + Hyper-V | minikube, Hyper-V | **yes** | **Hyper-V + Pro edition** | ✓ own | ~1 h | $0 | none |
| **D** VirtualBox VM + k3s | VirtualBox | **yes** | VirtualBox + VT-x | ✓ own | ~2 h | $0 | ~½ day |
| **E** Codespaces + k3d | **none** | **no** | GitHub/billing | ✓ own | ~15 min/dev | $0–115/dev | ~1 day |
| **F** Shared cloud k3s | Tailscale | no | cloud VM + tunnel | ✓ shared | ~3 h once | **$25–120 team** | ~2 h |
| **G** Replay mode | uv, git, node | no¹ | **none** | ✗ (simulated) | 30 min | $0 | **2–4 days** |

¹ admin only for GnuPG, and only if we don't hand them a portable GPG or keep the keys centrally.

**Recommended combination:**

1. **Today — A.** Two devs committing within the hour, nothing blocked on IT. Non-negotiable floor whatever else we pick.
2. **This week — B1.** ~1 hour, $0, gives them live Prometheus/Jaeger/Loki/Grafana off my cluster. Read-only, so no blast-radius conflict.
3. **Next sprint — G.** The durable answer. Fixes onboarding permanently *and* de-risks the client demo, which is worth the 2–4 days on its own.
4. **If any budget clears — F instead of B.** €25/month removes my laptop as the team SPOF and makes the demo portable. I'd push for this.
5. **C only if IT confirms Hyper-V is allowed** and only for a dev who genuinely needs their own cluster. Zero code change, so it costs nothing to try — but it likely fails for the same reason WSL did.

**E (Codespaces)** is the right answer if IT ends up blocking tunnels and cloud VMs too — it's the only option needing literally nothing on the endpoint — but at ~$115/dev/month for the 8-core the cluster requires, F+A is better value for the same money. The 2-core no-cluster Codespace (A-in-the-cloud, ~60 h/month free) is worth keeping in the back pocket as a zero-install fallback.

---

## 5. Open questions to resolve before choosing

1. **Why exactly is WSL blocked?** Named-feature block (WSL only) vs. hypervisor-platform / virtualization block. **This single answer decides C and D.** WSL2 and Hyper-V share the same hypervisor stack; VirtualBox does not.
2. **Windows edition on the two laptops?** Hyper-V needs Pro/Enterprise/Education. Home rules out C entirely.
3. **Do they have local admin at all,** or does every install need a ticket? Determines whether A is 30 minutes or three days of waiting.
4. **Is Tailscale (or any WireGuard tunnel) installable?** Gates B and F.
5. **Is a non-corporate cloud VM acceptable** for a POC on synthetic data? Gates F. (`CLAUDE.md` already forbids real customer data, so this should be an easy yes.)
6. **Codespaces billing** — personal account under `UbiquotousPanda`, or move the repo to a Zensar org with a Codespaces budget? Gates E.
7. **git-crypt / GPG:** does each dev generate their own key (needs GnuPG, admin once) and I run `scripts/secrets/add-teammate.ps1`, or do I hand them a `.env` out-of-band for now? Blocks day 1 either way, so decide first.

---

## 6. Day-1 runbook for option A (no WSL, no cluster)

Give this to both devs as-is. Should be ~30 minutes of wall-clock.

```powershell
# 1. Tooling — all user-scope, no admin
winget install --scope user --id astral-sh.uv
winget install --scope user --id Git.Git
winget install --scope user --id OpenJS.NodeJS
# refresh PATH in the current shell:
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
uv --version; git --version; node --version

# 2. Clone
cd C:\Projects
git clone https://github.com/UbiquotousPanda/AIops.git AIops
cd AIops

# 3. Python — uv-managed, sidesteps the BitLocker'd D:\python.exe gotcha
uv python install 3.12
[Environment]::SetEnvironmentVariable("UV_PYTHON", "3.12", "User")
uv sync --extra dev --extra ui --extra embeddings

# 4. Secrets — either git-crypt unlock (after Gaurav adds your GPG key), or
#    take .env from Gaurav out-of-band for day 1. Then:
#      - leave AIOPS_PROMETHEUS_URL / _JAEGER_URL / _LOKI_URL / _GRAFANA_URL unset
#      - AIOPS_LLM_PROVIDER=anthropic   (cloud — works fine)
#      - AIOPS_USE_MOCK_ITSM=false      (ServiceNow PDI is cloud — works fine)

# 5. Prove the toolchain — expect 775 passed, 2 known failures (see §2)
uv run pytest -m "not integration and not llm"
uv run python -m evals.harness            # expect overall_pass_rate: 1.0
uv run ruff check .

# 6. Build the SPAs (start.ps1 normally does this; we're skipping start.ps1
#    because it probes for a cluster we don't have)
cd demo\dashboard;      npm install; npm run build; cd ..\..
cd demo\combined-ui;    npm install; npm run build; cd ..\..
cd demo\classifier-ui;  npm install; npm run build; cd ..\..
cd demo\hitl-ui;        npm install; npm run build; cd ..\..

# 7. Run the server (NOT start.ps1 — it throws on an unreachable k8s API)
uv run python -m uvicorn demo.ui.server:app --port 8765
```

Then open <http://localhost:8765/dashboard/> and fire the chain:

```powershell
Invoke-RestMethod -Method POST http://localhost:8765/api/triage/fixture/payment_cpu_spike -TimeoutSec 90 |
    ConvertTo-Json -Depth 4
```

Expect a full `verdict` / `ticket` / `classification` / `persisted` payload, a real INC in the ServiceNow PDI, and the verdict on the dashboard's AI Reasoning tab. **Verified working with no cluster and no kubeconfig** (§2).

Expected-to-fail-and-that's-fine: the Failure Injection panel's Inject button (502), Alert Stream, Active alerts, Severity mix, Service Graph, and anything linking to :8080 or Grafana.

**Follow-up work this doc implies** (small, worth doing whichever option wins): commit a `.env.nocluster` preset; add `start-lite.ps1`; add a "no cluster" path to `ONBOARDING.md` §2 so `wsl --install` is no longer step one; fix the two failing tests in §2.

---

## Sources

- [minikube Hyper-V driver — requirements](https://minikube.sigs.k8s.io/docs/drivers/hyperv/)
- [kubernetes-in-codespaces (k3d devcontainer template)](https://github.com/codespaces-lab/kubernetes-in-codespaces)
- [Codespaces for Free and Pro accounts — included core-hours](https://github.blog/changelog/2022-11-09-codespaces-for-free-and-pro-accounts/)
- [Troubleshooting included Codespaces usage — core-hour multipliers](https://docs.github.com/en/codespaces/troubleshooting/troubleshooting-included-usage)
- [GitHub pricing calculator](https://github.com/pricing/calculator)
- [Gitpod SaaS deprecation — what are your options](https://www.harness.io/blog/gitpod-saas-is-being-deprecated-what-are-your-options)
- [DevPod](https://devpod.sh) · [Coder](https://coder.com)
