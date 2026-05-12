# One-command bring-up for the Adaptive AIOps demo.
#
# Does (in order):
#   1. Ensures the k3d cluster 'aiops' is running
#   2. Brings up port-forwards for Prometheus (9090), Jaeger (16686), frontend-proxy (8080)
#   3. Starts the FastAPI demo server (demo/ui/server.py) at http://localhost:8765
#   4. Opens the browser
#
# Stop with: .\stop.ps1

[CmdletBinding()]
param(
    [string]$Namespace = 'otel-demo',
    [int]$UiPort = 8765,
    [string]$LlmProvider = '',   # leave empty to let .env drive AIOPS_LLM_PROVIDER
    [string]$LlmModel = ''       # leave empty to let .env drive AIOPS_LLM_MODEL
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
Write-Step 1 "checking k3d cluster 'aiops'..."
$clusterListing = (k3d cluster list 2>&1 | Out-String)
if ($clusterListing -notmatch '\baiops\b') {
    throw "k3d cluster 'aiops' does not exist. Create it first with: k3d cluster create aiops --servers 1 --agents 2 --wait"
}
$wsl = (wsl --list --verbose 2>&1 | Out-String)
if ($wsl -notmatch 'r.a.n.c.h.e.r.-.d.e.s.k.t.o.p') {
    Write-Warning "WSL distro 'rancher-desktop' not visible -- make sure Rancher Desktop is running."
}
try {
    kubectl get nodes --request-timeout=5s | Out-Null
    Write-Host "    cluster API reachable" -ForegroundColor Green
} catch {
    Write-Host "    cluster API unreachable; attempting 'k3d cluster start aiops'..." -ForegroundColor Yellow
    k3d cluster start aiops | Out-Null
    Start-Sleep -Seconds 10
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

# --- 2.5 ensure dashboard build exists ---
$dashDir  = Join-Path $RepoRoot 'demo\dashboard'
$dashDist = Join-Path $dashDir 'dist\index.html'
if (Test-Path $dashDir) {
    if (-not (Test-Path $dashDist)) {
        Write-Step '2b' "building React dashboard (first run, ~30 s)..."
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Write-Warning "npm not found; skipping dashboard build. /dashboard will 503."
        } else {
            Push-Location $dashDir
            try {
                if (-not (Test-Path 'node_modules')) {
                    Write-Host "    npm install ..." -ForegroundColor DarkGray
                    npm install --no-audit --no-fund --silent 2>&1 | Out-Null
                }
                Write-Host "    npm run build ..." -ForegroundColor DarkGray
                npm run build --silent 2>&1 | Out-Null
                if (Test-Path $dashDist) {
                    Write-Host "    dashboard built -> demo/dashboard/dist/" -ForegroundColor Green
                } else {
                    Write-Warning "dashboard build did not produce dist/index.html"
                }
            } finally { Pop-Location }
        }
    } else {
        Write-Host "    dashboard already built" -ForegroundColor DarkGray
    }
}

# --- 3. ui server ---
Write-Step 3 "starting demo UI server on http://localhost:$UiPort ..."
if ($LlmProvider) { $env:AIOPS_LLM_PROVIDER = $LlmProvider }
if ($LlmModel)    { $env:AIOPS_LLM_MODEL    = $LlmModel    }
$uiJob = Start-Job -Name 'pf-ui-server' -ScriptBlock {
    param($repoRoot, $port, $llmProvider, $llmModel)
    Set-Location $repoRoot
    # Only set if explicitly passed; otherwise let uv-loaded .env drive.
    if ($llmProvider) { $env:AIOPS_LLM_PROVIDER = $llmProvider }
    if ($llmModel)    { $env:AIOPS_LLM_MODEL    = $llmModel    }
    uv run uvicorn demo.ui.server:app --host 127.0.0.1 --port $port
} -ArgumentList $RepoRoot, $UiPort, $LlmProvider, $LlmModel
$providerNote = if ($LlmProvider) { "LLM provider: $LlmProvider (overrides .env)" } else { "LLM provider: from .env" }
Write-Host "    started uvicorn (job $($uiJob.Id))  [$providerNote]" -ForegroundColor Green

# --- 4. wait for /api/health then open browser ---
Write-Step 4 "waiting for the UI server to come up..."
$deadline = (Get-Date).AddSeconds(45)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest "http://localhost:$UiPort/api/health" -UseBasicParsing -TimeoutSec 3
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
Write-Host "  Dashboard:  http://localhost:$UiPort/dashboard/   (React)"
Write-Host "  Demo UI:    http://localhost:$UiPort/             (vanilla)"
Write-Host "  Grafana:    http://localhost:8080/grafana/"
Write-Host "  Jaeger UI:  http://localhost:8080/jaeger/ui/"
Write-Host "  flagd UI:   http://localhost:8080/feature/"
Write-Host ''
Write-Host "Manage background jobs:"
Write-Host "  Get-Job -Name 'pf-*'                       # see what's running"
Write-Host "  Get-Job -Name 'pf-*' | Receive-Job -Keep   # tail logs"
Write-Host "  .\stop.ps1                                  # tear it all down"
