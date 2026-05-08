# Team Onboarding — Adaptive AIOps POC

This is the **first thing every team member reads**. It walks you from a fresh corporate laptop to a working development environment and tells you what to do next.

The canonical project context lives in [`README.md`](README.md) and [`CLAUDE.md`](CLAUDE.md); the deep narrative lives in [`docs/poc_aiops_onboarding_guide.docx`](docs/poc_aiops_onboarding_guide.docx). This document is shorter and more practical: **what to install, in what order, with whose permissions.**

Target environment: Windows 11 Pro, Intel Ultra 5, 16 GB RAM, no cloud access, no Docker. Local Kubernetes via **Rancher Desktop**.

---

## 0. Where each team member is right now (status check)

Run these three commands in PowerShell. They are **all no-admin** and tell you what's left to do.

```powershell
wsl --status
systeminfo | Select-String "Hyper-V|Virtualization"
Get-Command kubectl, helm, uv -ErrorAction SilentlyContinue
```

Match your output against this table:

| What you see | Means | Next step |
|---|---|---|
| `wsl --status` says **"WSL2 is not supported with your current machine configuration"** | WSL command is installed but Windows features (VM Platform / WSL) are off | Section 1 — admin needed once |
| `systeminfo` shows **"A hypervisor has been detected"** or **"Virtualization-based security: Running"** | Hardware virtualization is on. Good. | Continue with Section 1 |
| `systeminfo` shows **"Virtualization Enabled In Firmware: No"** | BIOS virtualization is off — separate IT ticket | Section 7 — Plan B |
| `kubectl`, `helm`, `uv` all return paths | Tools already installed | Skip Section 2 |
| Any of those tools missing | Install user-scope | Section 2 — no admin |

> **Reference machine (Chinmay's, 2026-05-08):** end-to-end Phase 0 is verified — WSL2 + Rancher Desktop k3s + OTel demo + failure injection all green. Your team's other three laptops still need to walk through this guide.

---

## 1. One-time admin steps (IT does this once per laptop)

Two actions, both from an **elevated PowerShell** ("Run as administrator"):

```powershell
# Enables both "Virtual Machine Platform" and "Windows Subsystem for Linux"
# Windows features. Adds the kernel. Schedules a reboot.
wsl.exe --install --no-distribution

# Reboot when prompted — the features only take effect after a restart.
Restart-Computer
```

After reboot, verify (no admin needed):

```powershell
wsl --status
# Expected:
#   Default Version: 2
#   Default Distribution: <none yet, that's fine>
```

If you also need Rancher Desktop installed (`winget` finds it as `SUSE.RancherDesktop`):

```powershell
winget install --id SUSE.RancherDesktop --source winget
```

That covers admin's part. **Everything from here is no-admin.**

### What to put on the IT ticket

> Request: enable WSL2 + install Rancher Desktop (one-time, four laptops).
>
> 1. `wsl.exe --install --no-distribution`
> 2. Reboot
> 3. `winget install --id SUSE.RancherDesktop`
>
> Rancher Desktop is published by SUSE under Apache 2.0; no telemetry by default. Project requires local Kubernetes; cloud sandbox is not approved for this team. No further admin needed after these three steps.

---

## 2. Per-developer setup (no admin, ~15 min)

### 2.1 Install user-scope CLI tools

```powershell
winget install --scope user --id astral-sh.uv
winget install --scope user --id Kubernetes.kubectl
winget install --scope user --id Helm.Helm
winget install --scope user --id Git.Git           # if not already present
winget install --scope user --id k6.k6             # optional, for load testing later
```

Confirm:

```powershell
uv --version
kubectl version --client
helm version
```

### 2.2 First-time Rancher Desktop setup

1. Launch **Rancher Desktop** from the Start menu.
2. On first launch:
   - **Container Engine:** `containerd` (faster, lighter than dockerd for this project).
   - **Kubernetes:** **enabled**, latest stable.
   - **Memory:** 6 GB. **CPUs:** 4.
   - **WSL Integration:** leave default.
3. Wait for the status indicator (bottom-left of the UI) to go green — this can take 5–10 minutes the first time as it downloads the k3s image.

Verify k3s is up:

```powershell
kubectl config get-contexts            # 'rancher-desktop' should be listed and current
kubectl get nodes                      # one node, STATUS Ready
helm version                           # already shipped with Rancher Desktop
```

### 2.3 Clone the repo and install Python deps

```powershell
git clone <repo-url> C:\Projects\AIops    # if you haven't already
cd C:\Projects\AIops
uv sync --extra dev
```

### 2.4 Smoke-test the platform seams (no cluster needed)

```powershell
$env:AIOPS_LLM_PROVIDER = "stub"        # uses the deterministic stub LLM
uv run pytest                            # 11 tests should pass
uv run python -m evals.harness           # phase0=true, pass_rate=1.0
uv run python -m demo.failure_injection.inject --list   # 3 scenarios listed
```

If those four steps pass, your **dev environment is ready**.

### 2.5 Bring up the demo cluster

```powershell
.\infra\bootstrap.ps1
```

This is idempotent — re-run any time. It uses the running Rancher Desktop k3s, adds the OpenTelemetry Helm repo, installs the demo into the `otel-demo` namespace, and waits for the frontend-proxy pod to be Ready. First install takes ~5–10 minutes (pulls ~30 images); subsequent runs are fast and skip Helm if the demo is already healthy.

After it returns, run the port-forward command it prints. The chart ships **one** unified ingress that exposes Webstore + Grafana + Jaeger + Loadgen + Feature-Flags UI through a single port-forward:

```powershell
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080
```

Then in your browser:
- <http://localhost:8080/> — Webstore
- <http://localhost:8080/grafana/> — Grafana (admin / admin)
- <http://localhost:8080/jaeger/ui/> — Jaeger UI
- <http://localhost:8080/loadgen/> — Load generator UI
- <http://localhost:8080/feature/> — flagd / feature-flag UI

### 2.6 Trigger a failure scenario

```powershell
uv run python -m demo.failure_injection.inject slow-product-catalog
# Watch latency rise in Grafana / traces in Jaeger
uv run python -m demo.failure_injection.inject --clear
```

---

## 3. Free-tier accounts to set up while waiting on IT

These are all **no-admin, no-cost**, signed up with your work email.

| Service | Sign up at | What we need it for |
|---|---|---|
| **Anthropic API** | <https://console.anthropic.com> | Default LLM provider (Phase 1+) |
| **ServiceNow PDI** | <https://developer.servicenow.com> | ITSM target for Auto-Ticketing agent |
| **PagerDuty Developer** | <https://developer.pagerduty.com> | On-call routing for Notification Router agent |
| **GitHub** (personal account is fine) | <https://github.com> | Source control |
| **Slack workspace** (team's choice) or Teams | n/a | Chat ops target for Notification Router |

Save credentials in your password manager. **Never commit `.env`.** The repo's `.env.example` is the template.

---

## 4. TODO list — work in parallel while environment is being set up

The cluster + agents are still ahead. **Don't wait — every TODO below can be done now.**

### 4.1 Everyone, week 1

- [ ] Run the three status-check commands in Section 0; share results in the team channel.
- [ ] Read [`README.md`](README.md), [`CLAUDE.md`](CLAUDE.md), and **your assigned agent's row** in `docs/Adaptive_AIOps_Agent_Catalog.xlsx`.
- [ ] Sign up for the four free-tier accounts in Section 3.
- [ ] Once tools are installed (Section 2.1), run `uv run pytest`. Should pass with the stub LLM.
- [ ] Skim [`docs/poc_aiops_onboarding_guide.docx`](docs/poc_aiops_onboarding_guide.docx) Parts 5–7. (Foundations, the 30 agents, tools.)

### 4.2 Phase-1 agent assignments (4 agents, 4 people)

POC guide §8.2 picks these as the Phase-1 backbone. One agent per person:

| Agent | Catalog ID | Owner | Status |
|---|---|---|---|
| Alert Triage | RA-001 | _assign_ | Not started |
| Auto-Ticketing | RA-003 | _assign_ | Not started |
| Notification Router | RA-005 | _assign_ | Not started |
| Log Correlation | RA-007 | _assign_ | Not started |

For each, the owner does these in week 1 (no cluster needed):

- [ ] Read the agent's row in `docs/Adaptive_AIOps_Agent_Catalog.xlsx` end-to-end (description, key features, primary tools, inputs, outputs, HITL level, KPI).
- [ ] Write a 1-page "what done looks like" doc in `agents/<phase>-<id>-<slug>/README.md`. Use the structure in [`agents/README.md`](agents/README.md).
- [ ] Hand-write **5 golden test cases** in `agents/<phase>-<id>-<slug>/evals/golden.json`. Use the format in [`evals/README.md`](evals/README.md). These force you to nail the input/output contract before writing prompt code.
- [ ] Identify the tool capabilities your agent needs (see [`aiops/tools/README.md`](aiops/tools/README.md)). If the capability isn't listed, add a row.

### 4.3 Demo environment owner

One person (rotating; could be the team lead) owns:

- [ ] Get `infra/bootstrap.ps1` updated for Rancher Desktop (drop `kind` step, use `rancher-desktop` kubeconfig context). I'll do this when the team confirms Rancher is running.
- [ ] Validate the three Phase-0 failure scenarios end-to-end: `slow-product-catalog`, `kafka-queue-buildup`, `currency-pod-kill`.
- [ ] Run `k6 run demo/load/baseline.js` for ~10 minutes and confirm steady-state metrics in Grafana.
- [ ] Check the truth file for each scenario against what you actually observe; tighten where the description doesn't match reality.

### 4.4 Eval / governance owner

One person owns:

- [ ] Add a fourth failure scenario (suggested: a database connection-pool exhaustion via flagd, mirrored by a truth file).
- [ ] Write integration tests for `aiops/policy/gate.py` covering all three autonomy levels with realistic action contexts.
- [ ] Verify the rules in `policies/hitl.rego` mirror `aiops/policy/gate.py::DEFAULT_LEVELS` exactly. Drift here is silent.
- [ ] Get `opa fmt` and `opa check` running locally so you can debug Rego before CI catches it.

### 4.5 Nice-to-have (anyone with spare time)

- [ ] Hand-curate 10–15 demo runbooks in `docs/runbooks/` for the OTel demo services. Use the format you'd find in a real customer's Confluence: title, when-to-run, steps, rollback, owner. This becomes the corpus the Knowledge Synthesizer agent retrieves over.
- [ ] Sketch a "what the demo will show in Phase 1 final" 3-bullet narrative. POC guide §9.10 says: write the demo narrative early so feature creep gets filtered against it.

---

## 5. Verification: green-light checklist before Phase 1 kicks off

Phase 1 starts when **every person on the team** can tick all of these. The reference machine (Chinmay's, 2026-05-08) has all eight machine-side items validated; the per-team items are still open.

- [x] `wsl --status` shows `Default Version: 2` with no errors. *(reference)*
- [x] Rancher Desktop UI shows green "Kubernetes is running." *(reference)*
- [x] `kubectl get nodes` returns a Ready node on the `rancher-desktop` context. *(reference)*
- [x] `uv run pytest` is green. *(reference: 11/11 passed)*
- [x] `uv run python -m evals.harness --ci --min-pass-rate 0.85` is green. *(reference: phase0=true, pass_rate=1.0)*
- [x] OTel demo Webstore is reachable at `http://localhost:8080/`. *(reference)*
- [x] Grafana is reachable at `http://localhost:8080/grafana/` (admin/admin). *(reference)*
- [x] Jaeger is reachable at `http://localhost:8080/jaeger/ui/`. *(reference)*
- [x] `slow-product-catalog` failure scenario triggered and cleared end-to-end. *(reference)*
- [ ] The other three team members complete the checklist on their laptops.
- [ ] The team has agreed on Phase-1 agent assignments (Section 4.2).
- [ ] At least one Anthropic API key is provisioned and tested with the gateway:
      ```powershell
      $env:AIOPS_LLM_PROVIDER = "anthropic"
      $env:ANTHROPIC_API_KEY = "<your key>"
      uv run python -c "from aiops.llm import Message, complete; print(complete([Message('user','say hi')]).text)"
      ```

When **everyone** can tick everything, you are at the start of Phase 1 (POC guide §8.2).

---

## 6. Working agreements

- **Communication:** every blocker that costs more than ~30 minutes goes in the team channel. Lurking and silently struggling is the single biggest week-1 anti-pattern (POC guide §10.2.1).
- **Code review:** every PR follows [`CONTRIBUTING.md`](CONTRIBUTING.md). The hard rules are: no direct vendor SDK imports, no HITL inside agent code, every new failure scenario ships with a truth file, every new agent ships with `evals/golden.json`. CI enforces all four.
- **No personal credentials in shared envs.** Each developer uses their own ServiceNow PDI, their own Anthropic key, their own PagerDuty account. Sharing makes audit impossible.
- **Notes file from day 1.** POC guide §10.2.2 — keep one. At end of week 2, send your notes to the team lead. Next batch of joiners reads them.

---

## 7. Troubleshooting

### Plan B if BIOS virtualization is genuinely off

If `systeminfo` shows `Virtualization Enabled In Firmware: No` (different from your case — yours is fine), IT must reboot into BIOS/UEFI and enable Intel VT-x or AMD-V. On corporate-managed laptops, this often means a remote firmware push by the endpoint-management team. Separate ticket, can take days. While waiting:

- Keep doing TODOs in Section 4 — none of them require a cluster.
- Whoever has admin first hosts a shared k3s cluster on their laptop; others connect via shared kubeconfig. Single point of failure but unblocks demos.
- POC guide §3.2 explicitly accepts running against canned fixtures instead of a live cluster. Phase 1 agents can be built and evaluated against recorded JSON without touching kubernetes.

### "Rancher Desktop won't start Kubernetes"

1. Open Rancher Desktop UI → **Troubleshooting** → **Factory Reset**. Choose "Keep cached images." This re-creates the WSL distro and almost always unsticks startup.
2. If it still fails, check `wsl --list --verbose` — you should see `rancher-desktop` and `rancher-desktop-data` distros, both Version 2, State Running.
3. Check Rancher's logs: `%APPDATA%\rancher-desktop\logs\` — `k3s.log` is the right file.

### "winget says it can't find Rancher Desktop"

`winget search "Rancher Desktop"` succeeded for us. If it fails for someone else:

- Update winget: `winget upgrade --id Microsoft.AppInstaller`.
- Or download the MSI directly from <https://rancherdesktop.io>.

### "uv sync is slow / fails behind corporate proxy"

```powershell
$env:HTTPS_PROXY = "http://your-corp-proxy:8080"
$env:UV_INDEX_URL = "https://pypi.org/simple"   # default; corporate mirror sometimes needed
uv sync --extra dev
```

If a corporate Python mirror is required, ask IT for the URL and put it in `UV_INDEX_URL`.

### "kubectl get nodes" hangs / returns nothing

Rancher Desktop hasn't finished starting. Check the UI status indicator (bottom-left). If it's still spinning after 10 minutes, factory-reset (above).

### "Helm install fails with image-pull errors on first try"

The OTel demo chart pulls 30+ images from `ghcr.io`. On first install, racing/timeouts are normal. Re-run the same `helm upgrade --install` command — it's idempotent and will continue from where it stopped.

### "Python subprocess kubectl errors with `unknown shorthand flag: 'n' in -n`"

**Symptom:** `uv run python -m demo.failure_injection.inject ...` fails with that error even though `kubectl -n otel-demo get pods` works fine in PowerShell directly.

**Cause:** Rancher Desktop ships a `kuberlr` wrapper at `C:\Program Files\Rancher Desktop\resources\resources\win32\bin\kubectl.exe`. The wrapper rejects standard kubectl args (like `-n`, `--client`, `--all-namespaces`) when invoked under Python `subprocess` — but works when invoked from PowerShell directly. Confirm with:

```powershell
& "C:\Program Files\Rancher Desktop\resources\resources\win32\bin\kubectl.EXE" --help
```

If the help only lists `-v, --verbosity Level` as a global flag, that's the wrapper.

**Fix:** Install the real kubectl alongside it.

```powershell
winget install --scope user --id Kubernetes.kubectl
```

The repo's `inject.py` already has a `_require_kubectl()` resolver that probes both kubectls and uses whichever responds correctly to `kubectl version --client=true`. So once the winget kubectl is on PATH, failure injection just works.

### "helm upgrade fails with `conflict with kubectl-patch using v1: .data.demo.flagd.json`"

**Symptom:** Re-running `.\infra\bootstrap.ps1` (or `helm upgrade`) after triggering a failure scenario errors out:

```
Error: UPGRADE FAILED: conflict occurred while applying object otel-demo/flagd-config /v1, Kind=ConfigMap:
Apply failed with 1 conflict: conflict with "kubectl-patch" using v1: .data.demo.flagd.json
```

**Cause:** Old versions of `inject.py` patched the `flagd-config` ConfigMap with kubectl's default field manager (`kubectl-patch`). That marks the field as owned by `kubectl-patch`, and Helm's server-side apply refuses to override it on subsequent upgrades. The fix shipped in `inject.py` uses `--field-manager=helm` so future patches don't poison the configmap. If you hit this on a cluster that was patched before the fix, recover with:

```powershell
# Clear any active failures first
uv run python -m demo.failure_injection.inject --clear

# Drop the configmap; Helm rollback will recreate it with helm's field manager
kubectl -n otel-demo delete configmap flagd-config
helm -n otel-demo rollback otel-demo

# Verify clean state
helm -n otel-demo ls    # STATUS should be 'deployed'
```

If `helm rollback` fails too, nuke and reinstall: `.\infra\teardown.ps1; .\infra\bootstrap.ps1`. Cached images make the second install fast (~1–2 min).

---

## 8. What's next once everyone is green

Phase 1 (POC guide §8.2) — first internal demo at the end of week 5. The agents to build are the four in Section 4.2. Each one is roughly:

1. Ship the `agent.py` skeleton with a stub `run(input)` that returns a placeholder.
2. Wire its prompt and tool calls. **Always through `aiops.llm` and `aiops.tools` — never vendor SDKs.**
3. Score against `evals/golden.json` until pass rate is ≥ 85%.
4. Demo end-to-end on a single failure scenario.

The four together compose into the demo: **trigger failure → Alert Triage collapses noise → Auto-Ticketing creates a SNOW ticket → Log Correlation pulls evidence → Notification Router pages the on-call.** That's the Phase-1 narrative.

---

*Last updated: 2026-05-08. If the install steps go stale, edit this file in the same PR that fixes them.*
