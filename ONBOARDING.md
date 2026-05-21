# Adaptive AIOps POC — Onboarding (Windows)

This doc gets you from a clean Windows laptop to a fully running Phase-0 demo cluster (OpenTelemetry Demo on Rancher Desktop k3s, with the failure-injection harness and eval suite green). Target audience: a new teammate joining the POC.

If something here disagrees with [`CLAUDE.md`](CLAUDE.md), CLAUDE.md wins — it's the architectural source-of-truth. This doc is the runbook.

---

## 0. What you're building

A local Kubernetes cluster running the **OpenTelemetry Demo** (a fake e-commerce store), plus a Python toolkit to inject failures and run agent eval scenarios against it. No cloud, no real customer data, no Docker (org policy).

Reference stack you'll end up with:

| Concern | Tool |
|---|---|
| Container runtime + k3s | Rancher Desktop |
| Demo app | OpenTelemetry Demo (Helm chart) |
| Metrics / Logs / Traces | Prometheus / Loki / Tempo (bundled by the demo) |
| Dashboards | Grafana (bundled) |
| Failure injection | OTel demo feature flags via `flagd-config` |
| Python deps | `uv` |
| LLM | Anthropic Claude (via `aiops/llm/`) |

---

## 1. Hardware & account checklist

Before you start, confirm:

- [ ] **Windows 10/11** laptop (ideally 11)
- [ ] **≥ 16 GB RAM** (the OTel demo uses ~3.5 GB inside k3s; Rancher Desktop wants 6 GB allocated to the VM)
- [ ] **≥ 20 GB free on the drive that will host WSL** (see §3 if your C: is tight)
- [ ] **Admin rights for the one-time installs** (Rancher Desktop + WSL2 enablement). Get IT to do this once if you don't have admin yourself.
- [ ] **GitHub account** with access to the repo
- [ ] **Anthropic API key** (only needed once you start writing agents — Phase 1+)

---

## 2. One-time installs (needs admin once)

Do these in order. After this section you should not need admin again.

### 2.1 Enable WSL2

PowerShell (Admin):

```powershell
wsl --install --no-distribution
```

Reboot if prompted. After reboot:

```powershell
wsl --set-default-version 2
wsl --status      # should show "Default Version: 2"
```

### 2.2 Install Rancher Desktop

Download from <https://rancherdesktop.io/> and install. On first launch:

- **Container Engine:** `containerd` (or `dockerd-mode` — either works)
- **Kubernetes:** Enabled
- **Settings → Virtual Machine → Hardware:** at least 6 GB RAM, 4 CPU
- **Settings → Kubernetes:** keep the bundled k3s version (current default is fine)

Wait until the system-tray icon shows *Kubernetes: running* (~30-60 seconds).

### 2.3 Sanity check

PowerShell (regular, not admin):

```powershell
wsl --list --verbose
# Should list both distros at VERSION 2:
#   rancher-desktop        Running   (the runtime — containerd + k3s)
#   rancher-desktop-data   Stopped   (data-only — bind-mounted into the runtime; "Stopped" is normal)
```

---

## 3. Disk space planning (relocate WSL to D: if C: is tight)

Rancher Desktop puts its WSL distros under `%LOCALAPPDATA%\rancher-desktop\` on C: by default. The OTel demo will balloon them to **~8-12 GB**. If you don't have that on C:, move them to another drive first. **No admin needed for this section.**

### 3.1 Check your situation

```powershell
Get-PSDrive C, D | Select-Object Name, @{N='UsedGB';E={[math]::Round($_.Used/1GB,1)}}, @{N='FreeGB';E={[math]::Round($_.Free/1GB,1)}}
```

If C: has < 15 GB free and D: has more, do §3.2. Otherwise skip to §4.

### 3.2 Move Rancher Desktop's WSL distros to D:

1. Quit Rancher Desktop from the tray.
2. Run:

   ```powershell
   wsl --shutdown
   mkdir D:\wsl\backup, D:\wsl\rancher-desktop, D:\wsl\rancher-desktop-data -Force

   # Export from C: (read from VHDX on C:, write to D:)
   wsl --export rancher-desktop      D:\wsl\backup\rancher-desktop.tar
   wsl --export rancher-desktop-data D:\wsl\backup\rancher-desktop-data.tar

   # Delete the C: copies — this is what actually frees C:
   wsl --unregister rancher-desktop
   wsl --unregister rancher-desktop-data

   # Re-import onto D:
   wsl --import rancher-desktop      D:\wsl\rancher-desktop      D:\wsl\backup\rancher-desktop.tar      --version 2
   wsl --import rancher-desktop-data D:\wsl\rancher-desktop-data D:\wsl\backup\rancher-desktop-data.tar --version 2
   ```

3. Restart Rancher Desktop from the Start menu. Wait for *Kubernetes: running*.
4. Verify the new location:

   ```powershell
   Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' | ForEach-Object {
       $p = $_ | Get-ItemProperty
       if ($p.DistributionName -like 'rancher-desktop*') {
           [PSCustomObject]@{ Name = $p.DistributionName; BasePath = $p.BasePath }
       }
   }
   # BasePath should start with D:\wsl\...
   ```

5. Once you've confirmed Rancher Desktop boots clean and `kubectl get nodes` works, delete the backup tarballs:

   ```powershell
   Remove-Item D:\wsl\backup\*.tar
   ```

---

## 4. Non-admin tool installs

PowerShell (regular). Each `winget` line installs to user scope — no admin needed.

```powershell
winget install --scope user --id Kubernetes.kubectl
winget install --scope user --id Helm.Helm
winget install --scope user --id astral-sh.uv
winget install --scope user --id k6.k6           # optional, for load tests
winget install --scope user --id Git.Git         # if you don't have it
```

**Important:** existing PowerShell windows do not pick up the new PATH. Either close and reopen PowerShell, or refresh in-session:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
```

Verify each tool resolves and reports a version:

```powershell
kubectl version --client
helm version
uv --version
git --version
```

> **About kubectl:** Rancher Desktop ships its own kubectl at `C:\Program Files\Rancher Desktop\resources\resources\win32\bin\kubectl.exe` — it's a `kuberlr` wrapper that rejects standard flags (`-n`, `--client`) when invoked from Python `subprocess`. We install the winget kubectl alongside; the failure-injection script automatically prefers the winget one when both are present.

---

## 5. Clone the repo and set up Python

```powershell
cd C:\Projects                            # or wherever you keep code
git clone <repo-url> AIops
cd AIops
```

### 5.1 Python — workaround for the BitLocker gotcha

If `D:\python.exe` is in your registry but BitLocker-locked, `uv` will fail to discover Python. Install uv's own managed Python and pin it:

```powershell
uv python install 3.12
$env:UV_PYTHON = "3.12"
# Persist for future shells:
[Environment]::SetEnvironmentVariable("UV_PYTHON", "3.12", "User")
```

### 5.2 Create the venv and install deps

```powershell
uv sync --extra dev
# Optional extras (install on demand):
#   uv sync --extra dev --extra llm-anthropic     # when you start writing agents
#   uv sync --extra dev --extra ui                # when you bring up the demo UI
```

This creates `.venv\` at the repo root. Activate when you need:

```powershell
.\.venv\Scripts\Activate.ps1
# Your prompt should now show (adaptive-aiops)
```

> Almost every command in the rest of this doc is prefixed `uv run` — that automatically uses the venv, so you don't have to remember to activate.

---

## 6. Bring up Phase 0

Make sure Rancher Desktop is running (tray icon shows *Kubernetes: running*). Then:

```powershell
.\infra\bootstrap.ps1
```

Idempotent — re-run any time. First run takes ~10 minutes because it pulls ~20 OTel demo images. What it does:

1. Verifies `kubectl` and `helm` are on PATH and k3s is reachable.
2. Pins the kube context to `rancher-desktop`.
3. Adds the `open-telemetry` Helm repo.
4. Installs the OTel demo into namespace `otel-demo` using [`demo/otel-demo/values.yaml`](demo/otel-demo/values.yaml).
5. Waits for the demo frontend pod to be Ready.

When it finishes, open a **second PowerShell window** and leave this running for the rest of your dev session:

```powershell
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080
```

That one port-forward exposes everything through the demo's built-in reverse proxy:

| URL | What |
|---|---|
| <http://localhost:8080/> | The webstore (the "fake customer" application) |
| <http://localhost:8080/grafana/> | Grafana — login `admin` / `admin` |
| <http://localhost:8080/jaeger/ui/> | Jaeger trace UI |

If you'd rather expose Prometheus and Jaeger on dedicated ports as well, `.\infra\port-forward.ps1` starts both as PowerShell background jobs.

---

## 7. Verify Phase 0 is green

Run these in the repo root. All should pass before you call setup done.

```powershell
# 7.1 Cluster reachable on the right context
kubectl config current-context        # rancher-desktop
kubectl get nodes                     # one Ready node

# 7.2 OTel demo deployed
helm list -n otel-demo                # otel-demo  deployed
kubectl get pods -n otel-demo         # all pods Running / Completed

# 7.3 Smoke tests (no cluster needed — they're pure-Python invariants)
uv run pytest

# 7.4 Eval harness — should print phase0=true, pass_rate=1.0
uv run python -m evals.harness

# 7.5 Lint
uv run ruff check .
```

If all five are clean, you're done with Phase 0 setup.

---

## 8. Use the demo (failure injection)

The whole point of Phase 0 is to have a system you can break on demand so agents have something real to react to.

### 8.1 List available scenarios

```powershell
uv run python -m demo.failure_injection.inject --list
```

You should see at least: `slow-product-catalog`, `kafka-queue-buildup`, `currency-pod-kill`. Each has a truth file under [`demo/truth_files/`](demo/truth_files/) describing the cause and the expected fix — these are the ground truth your agents will be graded against.

### 8.2 Inject a failure

```powershell
uv run python -m demo.failure_injection.inject slow-product-catalog
```

Now hit <http://localhost:8080/> in your browser — the product-catalog calls will be visibly slow. Open Grafana at `/grafana/` to see the latency spike on the service dashboard.

### 8.3 Clear the failure

```powershell
uv run python -m demo.failure_injection.inject --clear
```

System returns to baseline within a minute.

### 8.4 Add a new scenario

Every new scenario MUST ship with a truth file or the smoke test fails. See [`demo/truth_files/template.yaml`](demo/truth_files/template.yaml) and the README in that folder.

---

## 9. Daily workflow

> For the day-to-day cheat-sheet (start of day → port-forward → inject failure → end of day), see [`RUNNING.md`](RUNNING.md). The rest of this section is a short version of the same.

**Start of day:**

```powershell
# 1. Open Rancher Desktop from the tray (or Start menu); wait for green
# 2. Port-forward in its own window
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080
# 3. cd into the repo and you're ready
cd C:\Projects\AIops
```

**End of day:**

- Close the port-forward window (Ctrl+C, then close).
- Right-click Rancher Desktop tray → **Quit** to release RAM. The k3s state persists in the WSL VHDX — when you next start Rancher Desktop, the OTel demo is still installed.

You do **not** need to re-run `bootstrap.ps1` daily.

---

## 10. Tear down (when you want a clean slate)

```powershell
# Uninstalls the OTel demo helm release and deletes the otel-demo namespace.
# Does NOT touch Rancher Desktop or the k3s cluster itself.
.\infra\teardown.ps1
```

To start over from zero: `teardown.ps1` then `bootstrap.ps1`.

To wipe everything including the k3s cluster state: Rancher Desktop → **Troubleshooting → Reset Kubernetes**.

---

## 11. Common commands cheat sheet

```powershell
# Python
uv sync --extra dev                                    # install deps
uv run pytest                                          # all smoke tests
uv run pytest tests/test_smoke.py::test_hitl_gate_blocks_required_without_approver
uv run python -m evals.harness                         # all evals
uv run python -m evals.harness --agent ra-001-alert-triage
uv run python -m evals.harness --ci --min-pass-rate 0.85
uv run ruff check .
uv run ruff format .

# Failure injection
uv run python -m demo.failure_injection.inject --list
uv run python -m demo.failure_injection.inject slow-product-catalog
uv run python -m demo.failure_injection.inject --clear

# Infra
.\infra\bootstrap.ps1                                  # bring up OTel demo
.\infra\teardown.ps1                                   # tear it down
.\infra\port-forward.ps1                               # backend prometheus+jaeger forwards
kubectl -n otel-demo port-forward svc/frontend-proxy 8080:8080

# Cluster diagnostics
kubectl config current-context
kubectl get pods -n otel-demo
kubectl logs -n otel-demo -l app.kubernetes.io/component=frontend --tail=100
helm list -n otel-demo

# Frontend SPA builds (start.ps1 does these automatically — only needed
# if you're running uvicorn directly and want /dashboard or /classifier).
cd demo/dashboard      ; npm install ; npm run build ; cd ..\..
cd demo/classifier-ui  ; npm install ; npm run build ; cd ..\..
```

### Tailing the chatops audit log

The Notification Router writes every dispatch to [`demo/audit/chatops.jsonl`](demo/audit/). On PowerShell 5.1 you **must** pass `-Encoding UTF8` or em-dashes and other multi-byte characters show up as `â€"` mojibake (5.1's `Get-Content` default is CP1252; PS7+ defaults to UTF-8 but `start.ps1` documents 5.1 as the supported shell):

```powershell
# PowerShell 5.1 — explicit UTF-8
Get-Content demo/audit/chatops.jsonl -Wait -Encoding UTF8

# git-bash / WSL
tail -f demo/audit/chatops.jsonl
```

---

## 12. Troubleshooting (known gotchas, in order of how often you'll hit them)

### "Cannot reach the Kubernetes API" / `127.0.0.1:6443: connection refused`

Rancher Desktop isn't running, or k3s is still starting. Open Rancher Desktop, wait for the tray icon to show *Kubernetes: running*, retry.

### `helm install` fails: `flagd-config conflict with "kubectl-patch" using v1`

A prior `inject.py` patch poisoned the field-manager on the `flagd-config` ConfigMap. Two recovery paths:

```powershell
# Easy:
.\infra\teardown.ps1
.\infra\bootstrap.ps1

# Surgical:
kubectl delete configmap flagd-config -n otel-demo
helm rollback otel-demo -n otel-demo
```

The fix that prevents new poisoning is already in [`demo/failure_injection/inject.py`](demo/failure_injection/inject.py) — patches use `--field-manager=helm` so Helm's server-side apply doesn't reject them.

### Python error: `Error: unknown shorthand flag: 'n' in -n`

This means a Python subprocess called Rancher Desktop's `kuberlr` wrapper instead of real kubectl. Confirm winget kubectl is installed and on PATH ahead of the Rancher one:

```powershell
where.exe kubectl                       # the first one wins
```

If `C:\Program Files\Rancher Desktop\...` shows first, prepend the winget path. The repo's `inject.py` has a `_require_kubectl()` resolver that probes both.

### `uv` can't find Python / picks `D:\python.exe`

D: drive Python is BitLocker-locked. Install uv-managed Python:

```powershell
uv python install 3.12
[Environment]::SetEnvironmentVariable("UV_PYTHON", "3.12", "User")
# Open a fresh PowerShell window
```

### winget-installed tools aren't on PATH in the current shell

User-scope `winget install` updates the registry user PATH but already-open shells don't see it. Either reopen PowerShell or:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User")
```

### `ImagePullBackOff` / `Evicted` / disk-pressure on pods

The k3s node is out of disk. Usually because the WSL VHDX has grown and C: is full. Go back to §3.2 and move WSL to D:.

### `kubectl get pods` shows everything but the demo seems broken

Check the frontend logs first — most demo failures show up there cleanly:

```powershell
kubectl logs -n otel-demo -l app.kubernetes.io/component=frontend --tail=100
```

If a feature flag is stuck on, `kubectl get configmap flagd-config -n otel-demo -o jsonpath='{.data.demo\.flagd\.json}'` shows the current state.

---

## 13. What's next (Phase 1)

Phase 0 is the foundation; Phase 1 is the first real agent code. From the project plan:

- Assign owners to 4 agents: **Alert Triage (RA-001)**, **Auto-Ticketing (RA-003)**, **Notification Router (RA-005)**, **Log Correlation (RA-007)**. The contract for each is the corresponding row in [`docs/Adaptive_AIOps_Agent_Catalog.xlsx`](docs/Adaptive_AIOps_Agent_Catalog.xlsx).
- Each agent owner writes `agents/<phase>-<id>-<slug>/README.md` and 5 hand-written `evals/golden.json` cases **before** writing any prompt code. The evals are the spec.
- Provision Anthropic API keys per developer.
- Spin up free-tier sandboxes for ServiceNow PDI and PagerDuty developer accounts (used by RA-003 and RA-005).

When in doubt about an agent's behavior, contract, or KPI — the agent catalog is authoritative. Don't invent.

---

## 14. Where to read more

| What you want | Where |
|---|---|
| Architecture & design principles | [`CLAUDE.md`](CLAUDE.md) and [`docs/Adaptive_AIOps_Solution_Design.pptx`](docs/Adaptive_AIOps_Solution_Design.pptx) |
| Every agent's contract | [`docs/Adaptive_AIOps_Agent_Catalog.xlsx`](docs/Adaptive_AIOps_Agent_Catalog.xlsx) |
| AIOps concept primer (vocabulary, MTTR, SLO, toil…) | [`docs/aiops_onboarding_guide.docx`](docs/aiops_onboarding_guide.docx) |
| POC playbook (12-week plan, scope discipline) | [`docs/poc_aiops_onboarding_guide.docx`](docs/poc_aiops_onboarding_guide.docx) |
| Infra-specific README | [`infra/README.md`](infra/README.md) |
| Demo data and truth-file format | [`demo/truth_files/README.md`](demo/truth_files/README.md) |

If anything in this doc is wrong, broken, or stale, fix it in the same PR as whatever you were doing — don't open a "fix the onboarding doc" PR later. We'd rather have the doc drift one paragraph behind reality than be wrong in three places at once.
