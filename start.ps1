# One-command bring-up for the Adaptive AIOps demo.
#
# Does (in order):
#   1. Verifies Rancher Desktop k3s is reachable and the workloads are deployed
#   2. Port-forwards Prometheus (9090), Jaeger (16686), Grafana (3001), Loki (3100)
#   3. Starts the FastAPI demo server (demo/ui/server.py) at http://localhost:8765
#   4. Opens the browser
#
# This does NOT deploy anything. Two one-time installs come first:
#   .\infra\observability\install.ps1          # Prometheus/Grafana/Jaeger/Collector
#   cd demo\ecommerce; .\k8s\build-images.ps1  # then kubectl apply -f k8s\
# See demo/ecommerce/k8s/README.md.
#
# The ecommerce app itself needs no port-forward — it is exposed on NodePorts
# 30080-30083 (see demo/ecommerce/k8s/20-app.yaml).
#
# Pass -Fresh to wipe demo state before bring-up (DEMO-4 / #56):
#   - recovers every injected ecommerce failure scenario
#   - deletes data/state.db so verdict_id / cluster ids start at 1
#   - archives demo/audit/chatops.jsonl to chatops.jsonl.bak-<utc-timestamp>
# Default is off — iterative dev keeps state across runs.
#
# Stop with: .\stop.ps1

[CmdletBinding()]
param(
    # The observability stack moved out of the OTel Demo umbrella chart into
    # its own namespace (infra/observability/). Loki did NOT move: it is a
    # separate helm release, and reinstalling it would drop its PVC and every
    # log line collected so far.
    [string]$Namespace = 'observability',
    [string]$LokiNamespace = 'otel-demo',
    [string]$AppNamespace = 'ecommerce',
    # Precedence: explicit -UiPort > $env:AIOPS_UI_PORT > 8765. The env var
    # path lets ``.env`` (loaded inside the FastAPI process by aiops._dotenv)
    # and the start script agree without two places to keep in sync (#63).
    [int]$UiPort = $(if ($env:AIOPS_UI_PORT) { [int]$env:AIOPS_UI_PORT } else { 8765 }),
    [string]$LlmProvider = '',   # leave empty to let .env drive AIOPS_LLM_PROVIDER
    [string]$LlmModel = '',      # leave empty to let .env drive AIOPS_LLM_MODEL
    [string]$Context = 'rancher-desktop',  # set to '' to use current kube context
    [switch]$Fresh                          # wipe scenarios + state.db + chatops.jsonl
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot

function Write-Step($n, $msg) { Write-Host "[$n] $msg" -ForegroundColor Cyan }

# --- kubectl on PATH ---
# Always prepend the standalone kubectl if present. This shadows the broken
# Rancher Desktop wrapper that ships at C:\Program Files\Rancher Desktop\...
# and fails with a SHA-mismatch when it tries to auto-download from dl.k8s.io.
$standaloneKubectl = "$env:LOCALAPPDATA\Programs\kubectl"
if (Test-Path "$standaloneKubectl\kubectl.exe") {
    $env:Path = "$standaloneKubectl;$env:Path"
}
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw "kubectl not found on PATH and not at $standaloneKubectl\kubectl.exe"
}

# --- 1. cluster ---
Write-Step 1 "checking Rancher Desktop k3s..."
if ($Context) {
    $current = (kubectl config current-context 2>$null)
    if ($current) { $current = $current.Trim() }
    if ($current -ne $Context) {
        Write-Host "    switching kube context: $current -> $Context"
        kubectl config use-context $Context | Out-Null
    }
}
# Probe the API with a short timeout. Swap ErrorActionPreference because PS 5.1
# turns a native exe's stderr into a NativeCommandError under 'Stop'.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$null = & kubectl version --request-timeout=5s 2>&1
$probeExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP
if ($probeExit -ne 0) {
    Write-Host ''
    Write-Host "Cannot reach the Kubernetes API." -ForegroundColor Yellow
    Write-Host ''
    Write-Host "  Start Rancher Desktop from the Start menu and wait for the tray icon"
    Write-Host "  to show 'Kubernetes: running' (usually 30-60 seconds). Then re-run"
    Write-Host "  this script."
    throw "Rancher Desktop k3s API unreachable on context '$Context'."
}
Write-Host "    cluster API reachable" -ForegroundColor Green

# --- 1.2 workloads deployed? ---
# This script only port-forwards; it deploys nothing. Without these checks a
# missing install shows up as four silently-dead port-forward jobs and a
# dashboard full of empty panels, which is a genuinely confusing failure to
# debug. Fail here instead, naming the install command.
Write-Step '1.2' "checking workloads..."

$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$null = & kubectl -n $Namespace get svc prometheus-server 2>&1
$obsExit = $LASTEXITCODE
$null = & kubectl -n $AppNamespace get deploy order-service 2>&1
$appExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

if ($obsExit -ne 0) {
    Write-Host ''
    Write-Host "Observability stack not found in namespace '$Namespace'." -ForegroundColor Yellow
    Write-Host "  Install it once with:  .\infra\observability\install.ps1"
    throw "Missing observability stack (svc/prometheus-server in '$Namespace')."
}
Write-Host "    observability stack present in '$Namespace'" -ForegroundColor Green

if ($appExit -ne 0) {
    # A warning, not a throw: the agents, dashboard and HITL console are all
    # useful against a stack with no system under test. Only the scenario
    # pages need the app.
    # ASCII only inside PowerShell STRINGS in this file. It has no BOM, so
    # PS 5.1 decodes it as CP1252: an em-dash (UTF-8 e2 80 94) becomes the three
    # characters 'a-hat, euro, double-quote' — and that embedded quote
    # terminates the string early, silently swallowing the lines after it.
    # (Comments can keep their em-dashes; mangled text after a # is still a
    # comment.) Same CP1252 trap as the chatops.jsonl note in CLAUDE.md.
    Write-Warning "ecommerce app not found in namespace '$AppNamespace' - scenario inject/reset will fail."
    Write-Host "  Deploy it with:  cd demo\ecommerce; .\k8s\build-images.ps1; kubectl apply -f k8s\" -ForegroundColor DarkGray
} else {
    Write-Host "    ecommerce app present in '$AppNamespace'" -ForegroundColor Green
}

# --- 1.5 -Fresh cleanup (DEMO-4 / #56) ---
# Wipe demo state so the run starts from a clean slate. Three pieces of state
# survive a stop/start cycle by default:
#   - flagd-config in the cluster: scenarios stay flipped on across restarts
#   - data/state.db: verdict_id grows monotonically across sessions
#   - demo/audit/chatops.jsonl: yesterday's chatops entries get tailed by
#     `Get-Content -Wait` and mixed into today's feed
# -Fresh resets all three. Archived chatops logs land next to the live file
# as chatops.jsonl.bak-<utc-timestamp> and are gitignored (demo/audit/*).
if ($Fresh) {
    Write-Step '1.5' "fresh cleanup (-Fresh)..."

    # 1. Recover every injected failure scenario.
    # Was `demo.failure_injection.inject --clear`, which flipped flagd flags.
    # flagd shipped with the OTel Demo chart and is gone; faults are now env
    # vars and scaled-down StatefulSets, recovered through the ecommerce
    # failure_injection package. Needs the cluster up (verified above) but no
    # port-forward — it shells kubectl directly.
    Write-Host "    scenarios: recovering all injected faults..." -ForegroundColor DarkGray
    cmd /c "uv run python -c ""from demo.ui import scenario_provider as sp; sp.reset_all(sp.load())"" >NUL 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "    scenario reset returned exit $LASTEXITCODE; some faults may still be injected. Run 'uv run python -c ""from demo.ui import scenario_provider as sp; print(sp.reset_all(sp.load()))""' to see the error."
    } else {
        Write-Host "    scenarios: all faults recovered" -ForegroundColor Green
    }

    # 2. Drop data/state.db. ``init_db()`` recreates the schema on the first
    # FastAPI call, so the file comes back empty without manual intervention.
    $stateDb = Join-Path $RepoRoot 'data\state.db'
    if (Test-Path $stateDb) {
        Remove-Item $stateDb -Force
        Write-Host "    state.db: removed (init_db will recreate on first call)" -ForegroundColor Green
    } else {
        Write-Host "    state.db: not present, nothing to remove" -ForegroundColor DarkGray
    }

    # 3. Archive the chatops audit log so the WebSocket replay + tail don't
    # mix yesterday's entries into today's session. Use a UTC stamp so
    # archives sort lexicographically.
    $chatLog = Join-Path $RepoRoot 'demo\audit\chatops.jsonl'
    if (Test-Path $chatLog) {
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
        $bak = Join-Path $RepoRoot "demo\audit\chatops.jsonl.bak-$stamp"
        Move-Item $chatLog $bak
        New-Item -ItemType File -Path $chatLog | Out-Null
        Write-Host "    chatops.jsonl: archived -> $(Split-Path -Leaf $bak)" -ForegroundColor Green
    } else {
        # Touch the empty file so the WebSocket adapter can append on first
        # send without dealing with the missing-file edge case.
        New-Item -ItemType File -Path $chatLog -Force | Out-Null
        Write-Host "    chatops.jsonl: not present, created empty file" -ForegroundColor DarkGray
    }
}

# --- 2. port-forwards ---
Write-Step 2 "starting port-forwards..."
Get-Job -Name 'pf-*' -ErrorAction SilentlyContinue | Stop-Job -PassThru | Remove-Job | Out-Null

# NOTE the RemotePort values. prometheus-server and grafana listen on :80,
# not on the port they are forwarded to locally — the standalone charts differ
# from the OTel Demo subcharts here, and a 9090:9090 forward silently fails.
# Each entry carries its own namespace because Loki did not move.
$forwards = @(
    @{ Name = 'prometheus'; Ns = $Namespace;     Svc = 'svc/prometheus-server'; LocalPort = 9090;  RemotePort = 80    }
    @{ Name = 'jaeger';     Ns = $Namespace;     Svc = 'svc/jaeger';            LocalPort = 16686; RemotePort = 16686 }
    @{ Name = 'grafana';    Ns = $Namespace;     Svc = 'svc/grafana';           LocalPort = 3001;  RemotePort = 80    }
    @{ Name = 'loki';       Ns = $LokiNamespace; Svc = 'svc/loki';              LocalPort = 3100;  RemotePort = 3100  }
)
foreach ($fwd in $forwards) {
    $job = Start-Job -Name "pf-$($fwd.Name)" -ScriptBlock {
        param($ns, $svc, $local, $remote, $kubectlDir)
        if ($kubectlDir -and (Test-Path "$kubectlDir\kubectl.exe")) {
            $env:Path = "$kubectlDir;$env:Path"
        }
        kubectl port-forward -n $ns $svc "${local}:${remote}"
    } -ArgumentList $fwd.Ns, $fwd.Svc, $fwd.LocalPort, $fwd.RemotePort, $standaloneKubectl
    Write-Host ("    started {0,-12} {1,-14} -> http://localhost:{2}  (job {3})" -f $fwd.Name, $fwd.Ns, $fwd.LocalPort, $job.Id) -ForegroundColor Green
}
Start-Sleep -Seconds 3   # let pf jobs bind their ports

# --- 2.5 ensure SPA builds exist (React dashboard + standalone classifier UI) ---

# Freshness check: returns the LastWriteTime of the newest file that contributes
# to the build (src/**, plus the root config files Vite/Tailwind/TS consume).
# Used to decide whether dist/ is stale relative to source.
function Get-SpaSourceNewestMtime($dir) {
    $sources = @()
    $srcDir = Join-Path $dir 'src'
    if (Test-Path $srcDir) {
        $sources += Get-ChildItem -Path $srcDir -Recurse -File -ErrorAction SilentlyContinue
    }
    $configFiles = @(
        'index.html', 'package.json', 'package-lock.json',
        'tailwind.config.js', 'postcss.config.js',
        'vite.config.ts', 'vite.config.js',
        'tsconfig.json', 'tsconfig.node.json'
    )
    foreach ($name in $configFiles) {
        $p = Join-Path $dir $name
        if (Test-Path $p) { $sources += Get-Item $p }
    }
    if ($sources.Count -eq 0) { return $null }
    return ($sources | Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
}

function Invoke-SpaBuild($name, $dir, $missingMsg) {
    $distIndex = Join-Path $dir 'dist\index.html'
    if (-not (Test-Path $dir)) { return }

    # Decide why (or whether) we're building. Three states:
    #   missing — dist/index.html absent → first-run build
    #   stale   — newest source mtime > dist mtime → incremental rebuild
    #   fresh   — dist is up-to-date → skip
    $reason = $null
    $stamp  = Join-Path $dir 'dist\.built-commit'
    if (-not (Test-Path $distIndex)) {
        $reason = 'first run, ~30 s'
    } else {
        $distMtime = (Get-Item $distIndex).LastWriteTime
        $srcMtime  = Get-SpaSourceNewestMtime $dir
        if ($srcMtime -and $srcMtime -gt $distMtime) {
            $reason = 'source changed since last build'
        } elseif ($script:GitHead) {
            # Reliable post-pull check: a `git pull`/`checkout` rewrites source
            # files but git does NOT preserve mtimes in a way the check above can
            # trust — so a freshly-pulled dashboard can look "up-to-date" and the
            # updates (e.g. agentCatalog edits) never render. Rebuild whenever the
            # committed code differs from the commit this dist was built from.
            $builtFrom = if (Test-Path $stamp) { (Get-Content $stamp -Raw).Trim() } else { '' }
            if ($builtFrom -ne $script:GitHead) {
                $reason = 'code changed since last build (git pull/checkout)'
            }
        }
    }
    if (-not $reason) {
        Write-Host "    $name already built (up-to-date)" -ForegroundColor DarkGray
        return
    }

    Write-Step '2b' "building $name ($reason)..."
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        Write-Warning "npm not found; skipping $name build. $missingMsg"
        return
    }
    Push-Location $dir
    try {
        # Run npm via cmd.exe so its stderr chatter doesn't get wrapped as a
        # PS 5.1 NativeCommandError and tripped by $ErrorActionPreference = 'Stop'.
        if (-not (Test-Path 'node_modules')) {
            Write-Host "    npm install ($name) ..." -ForegroundColor DarkGray
            cmd /c "npm install --no-audit --no-fund --silent >NUL 2>&1"
        }
        Write-Host "    npm run build ($name) ..." -ForegroundColor DarkGray
        cmd /c "npm run build --silent >NUL 2>&1"
        if (Test-Path $distIndex) {
            # Stamp the commit this dist was built from so the next start can tell
            # whether a pull/checkout has since changed the code (see staleness
            # check above). dist/ is gitignored, so this stays machine-local.
            if ($script:GitHead) {
                Set-Content -Path $stamp -Value $script:GitHead -NoNewline -Encoding ascii
            }
            Write-Host "    $name built -> $(Resolve-Path -Relative $distIndex)" -ForegroundColor Green
        } else {
            Write-Warning "$name build did not produce dist/index.html"
        }
    } finally { Pop-Location }
}

$dashDir       = Join-Path $RepoRoot 'demo\dashboard'
$dashDist      = Join-Path $dashDir 'dist\index.html'
$classifierDir = Join-Path $RepoRoot 'demo\classifier-ui'
$combinedDir   = Join-Path $RepoRoot 'demo\combined-ui'
$hitlDir       = Join-Path $RepoRoot 'demo\hitl-ui'

# Current commit — lets each SPA build detect "the code changed since I was last
# built" after a git pull/checkout, which the mtime check alone can miss. Empty
# string if git isn't available, in which case we fall back to the mtime check.
$script:GitHead = ''
try { $script:GitHead = (& git -C $RepoRoot rev-parse HEAD 2>$null) } catch { $script:GitHead = '' }
if ($script:GitHead) { $script:GitHead = $script:GitHead.Trim() }

Invoke-SpaBuild 'React dashboard'   $dashDir       '/dashboard/ will 503.'
Invoke-SpaBuild 'classifier UI'     $classifierDir '/classifier will 503.'
Invoke-SpaBuild 'combined UI'       $combinedDir   '/combined will 503.'
Invoke-SpaBuild 'HITL approver UI'  $hitlDir       '/hitl will 503.'

# --- 2c. ensure the right extras are synced into .venv ---
# Without `--extra ui`, `uv run uvicorn` silently falls back to a uvicorn
# elsewhere on PATH whose site-packages is missing httpx etc.
# Without `--extra llm-<provider>`, the LLM gateway raises ImportError at
# runtime and every agent falls to its template / Tier-4 fallback (DEMO-2).
# `--extra embeddings` unlocks RA-001 dedup similarity + RA-002 historical
# similarity tiers. Idempotent and fast on re-runs.
Write-Step '2c' "syncing extras (dev, ui, embeddings, llm-<provider>) into .venv..."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv not found on PATH. Install uv: https://docs.astral.sh/uv/getting-started/installation/"
}

# Resolve the LLM provider: -LlmProvider param wins, then .env, then default.
function Get-EnvFileValue($path, $key) {
    if (-not (Test-Path $path)) { return $null }
    foreach ($line in Get-Content $path) {
        if ($line -match "^\s*$([Regex]::Escape($key))\s*=\s*(.+?)\s*(#.*)?$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}
$resolvedProvider = if ($LlmProvider) { $LlmProvider } else { Get-EnvFileValue (Join-Path $RepoRoot '.env') 'AIOPS_LLM_PROVIDER' }
if (-not $resolvedProvider) { $resolvedProvider = 'stub' }

$extras = @('--extra', 'dev', '--extra', 'ui', '--extra', 'embeddings')
$lp = $resolvedProvider.ToLowerInvariant()
if     ($lp -eq 'anthropic') { $extras += @('--extra', 'llm-anthropic') }
elseif ($lp -eq 'openai')    { $extras += @('--extra', 'llm-openai') }
elseif ($lp -eq 'ollama')    { $extras += @('--extra', 'llm-ollama') }
elseif ($lp -eq 'stub')      { }   # no SDK needed
else { Write-Warning "unknown AIOPS_LLM_PROVIDER='$resolvedProvider' - skipping llm-* extra" }
# NOTE: the anthropic SDK is pulled in transitively by `--extra ui` (the RCA
# Agent served here is pinned to the Azure Foundry Claude provider — see the
# `ui` extra in pyproject.toml), so it does not need a separate `--extra` here.
$extrasStr = $extras -join ' '
# Run uv via cmd so its stderr chatter doesn't get wrapped as a PS 5.1
# NativeCommandError under $ErrorActionPreference = 'Stop'.
cmd /c "uv sync $extrasStr --quiet >NUL 2>&1"
if ($LASTEXITCODE -ne 0) {
    throw "uv sync $extrasStr failed (exit $LASTEXITCODE). Run it manually to see the error."
}
Write-Host "    .venv has uvicorn + fastapi + llm SDK for '$resolvedProvider'" -ForegroundColor Green

# --- 3. ui server ---
Write-Step 3 "starting demo UI server on http://localhost:$UiPort ..."
if ($LlmProvider) { $env:AIOPS_LLM_PROVIDER = $LlmProvider }
if ($LlmModel)    { $env:AIOPS_LLM_MODEL    = $LlmModel    }
$uiJob = Start-Job -Name 'pf-ui-server' -ScriptBlock {
    param($repoRoot, $port, $llmProvider, $llmModel, $userProfile, $kubeconfig, $kubectlDir)
    Set-Location $repoRoot
    # ARCH-1 (issue #70): Start-Job's child PS process does NOT reliably
    # inherit USERPROFILE / KUBECONFIG from the parent on PS 5.1. Without
    # them, kubernetes.config.load_kube_config() raises
    # `ConfigException: Invalid kube-config file. No configuration found.`
    # and the feature_flags seam fails 502 on every /api/scenarios/* call.
    # Force-propagate them here.
    if ($userProfile) { $env:USERPROFILE = $userProfile }
    if ($kubeconfig)  { $env:KUBECONFIG  = $kubeconfig  }
    if ($kubectlDir -and (Test-Path "$kubectlDir\kubectl.exe")) {
        $env:Path = "$kubectlDir;$env:Path"
    }
    # Only set if explicitly passed; otherwise let uv-loaded .env drive.
    if ($llmProvider) { $env:AIOPS_LLM_PROVIDER = $llmProvider }
    if ($llmModel)    { $env:AIOPS_LLM_MODEL    = $llmModel    }
    # Launch uvicorn THROUGH python (`-m uvicorn`), not the `uvicorn.exe`
    # console-script shim. Some dev machines run a Windows Application Control
    # / Smart App Control policy that blocks the unsigned venv `uvicorn.exe`
    # (spawn fails with os error 4551), which silently kills the demo server so
    # the dashboard never comes up. `python.exe` is allowed, so `-m uvicorn`
    # sidesteps the block while running the identical server.
    uv run python -m uvicorn demo.ui.server:app --host 127.0.0.1 --port $port
} -ArgumentList $RepoRoot, $UiPort, $LlmProvider, $LlmModel, $env:USERPROFILE, $env:KUBECONFIG, $standaloneKubectl
$providerNote = if ($LlmProvider) { "LLM provider: $LlmProvider (overrides .env)" } else { "LLM provider: from .env" }
Write-Host "    started uvicorn (job $($uiJob.Id))  [$providerNote]" -ForegroundColor Green

# --- 4. wait for /api/health then open browser ---
Write-Step 4 "waiting for the UI server to come up..."
$deadline = (Get-Date).AddSeconds(60)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest "http://localhost:$UiPort/api/health" -UseBasicParsing -TimeoutSec 25
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Seconds 2 }
}
if ($ready) {
    Write-Host "    UI server is up." -ForegroundColor Green
    if (Test-Path $dashDist) {
        Start-Process "http://localhost:$UiPort/dashboard/"
    } else {
        Start-Process "http://localhost:$UiPort/"
    }
} else {
    Write-Warning "UI server didn't respond within 45 s. Check: Receive-Job -Name pf-ui-server -Keep"
}

Write-Host ''
Write-Host '== Up and running ==' -ForegroundColor Green
Write-Host "  Dashboard:    http://localhost:$UiPort/dashboard/   (RA-001)"
Write-Host "  Classifier:   http://localhost:$UiPort/classifier   (RA-002 SPA)"
Write-Host "  HITL console: http://localhost:$UiPort/hitl          (approver UI)"
Write-Host "  Demo UI:      http://localhost:$UiPort/             (vanilla)"
Write-Host "  API docs:     http://localhost:$UiPort/docs         (Swagger)"
Write-Host ''
Write-Host "  Storefront:   http://localhost:30080/            (ecommerce SUT)"
Write-Host "  user-service: http://localhost:30081/health"
Write-Host "  order-service:http://localhost:30082/health"
Write-Host ''
Write-Host "  Grafana:      http://localhost:3001/grafana/     (admin / admin)"
Write-Host "  Prometheus:   http://localhost:9090/"
Write-Host "  Alertmanager: kubectl -n $Namespace port-forward svc/prometheus-alertmanager 9093:9093"
Write-Host "  Jaeger UI:    http://localhost:16686/"
Write-Host "  Loki (raw):   http://localhost:3100/"
Write-Host ''
Write-Host "Manage background jobs:"
Write-Host "  Get-Job -Name 'pf-*'                       # see what's running"
Write-Host "  Get-Job -Name 'pf-*' | Receive-Job -Keep   # tail logs"
Write-Host "  .\stop.ps1                                  # tear it all down"
