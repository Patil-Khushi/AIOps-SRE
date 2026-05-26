# One-command bring-up for the Adaptive AIOps demo.
#
# Does (in order):
#   1. Ensures the k3d cluster 'aiops' is running
#   2. Brings up port-forwards for Prometheus (9090), Jaeger (16686), frontend-proxy (8080)
#   3. Starts the FastAPI demo server (demo/ui/server.py) at http://localhost:8765
#   4. Opens the browser
#
# Pass -Fresh to wipe demo state before bring-up (DEMO-4 / #56):
#   - resets every flag-driven scenario in flagd to ``off``
#   - deletes data/state.db so verdict_id / cluster ids start at 1
#   - archives demo/audit/chatops.jsonl to chatops.jsonl.bak-<utc-timestamp>
# Default is off — iterative dev keeps state across runs.
#
# Stop with: .\stop.ps1

[CmdletBinding()]
param(
    [string]$Namespace = 'otel-demo',
    [int]$UiPort = 8765,
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

    # 1. Clear every flag-driven scenario back to its 'off' variant.
    # The feature_flags seam talks to flagd-config via the kubernetes Python
    # client (ARCH-1 / #70) so it needs the cluster up — which we just
    # verified above — but no port-forward.
    Write-Host "    flagd: resetting all scenarios to 'off'..." -ForegroundColor DarkGray
    cmd /c "uv run python -m demo.failure_injection.inject --clear >NUL 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "    flagd reset returned exit $LASTEXITCODE; scenarios may still be on. Run 'uv run python -m demo.failure_injection.inject --clear' manually to see the error."
    } else {
        Write-Host "    flagd: all scenarios reset" -ForegroundColor Green
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

$forwards = @(
    @{ Name = 'prometheus';     Svc = 'svc/prometheus';     LocalPort = 9090;  RemotePort = 9090  }
    @{ Name = 'jaeger';         Svc = 'svc/jaeger';         LocalPort = 16686; RemotePort = 16686 }
    @{ Name = 'frontend-proxy'; Svc = 'svc/frontend-proxy'; LocalPort = 8080;  RemotePort = 8080  }
)
foreach ($fwd in $forwards) {
    $job = Start-Job -Name "pf-$($fwd.Name)" -ScriptBlock {
        param($ns, $svc, $local, $remote, $kubectlDir)
        if ($kubectlDir -and (Test-Path "$kubectlDir\kubectl.exe")) {
            $env:Path = "$kubectlDir;$env:Path"
        }
        kubectl port-forward -n $ns $svc "${local}:${remote}"
    } -ArgumentList $Namespace, $fwd.Svc, $fwd.LocalPort, $fwd.RemotePort, $standaloneKubectl
    Write-Host ("    started {0,-15} -> http://localhost:{1}  (job {2})" -f $fwd.Name, $fwd.LocalPort, $job.Id) -ForegroundColor Green
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
    if (-not (Test-Path $distIndex)) {
        $reason = 'first run, ~30 s'
    } else {
        $distMtime = (Get-Item $distIndex).LastWriteTime
        $srcMtime  = Get-SpaSourceNewestMtime $dir
        if ($srcMtime -and $srcMtime -gt $distMtime) {
            $reason = 'source changed since last build'
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
            Write-Host "    $name built -> $(Resolve-Path -Relative $distIndex)" -ForegroundColor Green
        } else {
            Write-Warning "$name build did not produce dist/index.html"
        }
    } finally { Pop-Location }
}

$dashDir       = Join-Path $RepoRoot 'demo\dashboard'
$dashDist      = Join-Path $dashDir 'dist\index.html'
$classifierDir = Join-Path $RepoRoot 'demo\classifier-ui'

Invoke-SpaBuild 'React dashboard'   $dashDir       '/dashboard/ will 503.'
Invoke-SpaBuild 'classifier UI'     $classifierDir '/classifier will 503.'

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
    uv run uvicorn demo.ui.server:app --host 127.0.0.1 --port $port
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
Write-Host "  Demo UI:      http://localhost:$UiPort/             (vanilla)"
Write-Host "  API docs:     http://localhost:$UiPort/docs         (Swagger)"
Write-Host "  Grafana:      http://localhost:8080/grafana/"
Write-Host "  Jaeger UI:    http://localhost:8080/jaeger/ui/"
Write-Host "  flagd UI:     http://localhost:8080/feature/"
Write-Host "  Prometheus:   http://localhost:9090/"
Write-Host "  Jaeger (raw): http://localhost:16686/"
Write-Host ''
Write-Host "Manage background jobs:"
Write-Host "  Get-Job -Name 'pf-*'                       # see what's running"
Write-Host "  Get-Job -Name 'pf-*' | Receive-Job -Keep   # tail logs"
Write-Host "  .\stop.ps1                                  # tear it all down"
