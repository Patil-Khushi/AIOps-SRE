# infra/bootstrap.ps1
# One-shot Phase-0 bootstrap for Rancher Desktop. Idempotent — re-run any time.
#
# What it does:
#   1. Verifies prerequisites (kubectl, helm) and that Rancher Desktop's k3s is reachable.
#   2. Adds the OpenTelemetry Helm repo.
#   3. Installs the OTel demo into the otel-demo namespace using demo/otel-demo/values.yaml.
#   4. Waits for the frontend pod to be Ready.
#   5. Prints the single port-forward command that exposes frontend / grafana / jaeger.

[CmdletBinding()]
param(
    [string]$Namespace = 'otel-demo',
    [string]$ChartVersion = '',                # empty = latest
    [string]$LokiChartVersion = '6.24.0',      # grafana/loki chart pin (RA-007 / #220)
    [string]$Context     = 'rancher-desktop',  # set to '' to use current context
    [switch]$Force                             # skip the already-healthy check and run helm upgrade unconditionally
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Require-Tool($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "Required tool '$name' not on PATH. See ONBOARDING.md section 2."
    }
}

Write-Host "==> Checking prerequisites" -ForegroundColor Cyan
Require-Tool kubectl
Require-Tool helm

# Pin the kube context so we don't accidentally install into a stray cluster.
if ($Context) {
    $current = (kubectl config current-context 2>$null).Trim()
    if ($current -ne $Context) {
        Write-Host "    switching kube context: $current -> $Context"
        kubectl config use-context $Context | Out-Null
    }
}

Write-Host "==> Verifying Rancher Desktop k3s is reachable"
# Probe the API with a short timeout. We intentionally swap ErrorActionPreference around the call
# because PowerShell 5.1 turns a native exe's stderr into a NativeCommandError under 'Stop',
# which would terminate before our friendly message below.
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$null = & kubectl version --request-timeout=5s 2>&1
$probeExit = $LASTEXITCODE
$ErrorActionPreference = $prevEAP

if ($probeExit -ne 0) {
    Write-Host ""
    Write-Host "Cannot reach the Kubernetes API." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Start Rancher Desktop from the Start menu and wait for the tray icon"
    Write-Host "  to show 'Kubernetes: running' (usually 30-60 seconds). Then re-run"
    Write-Host "  this script."
    Write-Host ""
    Write-Host "  If Rancher Desktop is already running, check that"
    Write-Host "  'kubectl config current-context' returns 'rancher-desktop' and that"
    Write-Host "  k3s is enabled (Settings -> Kubernetes)."
    exit 1
}

Write-Host "==> Adding OpenTelemetry + Grafana Helm repos"
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts 2>$null | Out-Null
helm repo add grafana https://grafana.github.io/helm-charts 2>$null | Out-Null
helm repo update | Out-Null

# Skip the install if the demo is already healthy — keeps re-runs fast. The
# helm call below uses --server-side=true --force-conflicts so it survives
# stale field managers left by inject.py / past kubectl-applies, but skipping
# the upgrade entirely is still faster when nothing has changed. Pass -Force
# to override and run helm upgrade unconditionally.
$alreadyHealthy = $false
if (-not $Force) {
    $existing = helm -n $Namespace ls --filter '^otel-demo$' -o json 2>$null
    if ($existing -and $existing.Trim() -ne '[]') {
        $status = ($existing | ConvertFrom-Json)[0].status
        if ($status -eq 'deployed') {
            $frontendReady = (kubectl -n $Namespace get pod -l app.kubernetes.io/component=frontend-proxy `
                -o "jsonpath={.items[0].status.conditions[?(@.type=='Ready')].status}" 2>$null)
            if ($frontendReady -eq 'True') {
                $alreadyHealthy = $true
            }
        }
    }
}

if ($alreadyHealthy) {
    Write-Host "==> otel-demo already deployed and frontend-proxy is Ready; skipping helm install."
    Write-Host "    To force a re-install: .\infra\bootstrap.ps1 -Force"
    Write-Host "    Or wipe and reinstall:  .\infra\teardown.ps1; .\infra\bootstrap.ps1"
} else {
    Write-Host "==> Installing OTel demo into namespace '$Namespace'"
    $valuesPath = Join-Path $repoRoot 'demo/otel-demo/values.yaml'
    $helmArgs = @(
        'upgrade', '--install', 'otel-demo', 'open-telemetry/opentelemetry-demo',
        '--namespace', $Namespace, '--create-namespace',
        '--values', $valuesPath,
        # Server-side apply with --force-conflicts lets helm take ownership of
        # fields that earlier kubectl-apply/kubectl-patch invocations claimed
        # (e.g. flagd-config .data.demo.flagd.json after inject.py runs, or
        # the prometheus configmap after manual edits). Without these flags,
        # subsequent upgrades fail with "Apply failed with N conflicts".
        '--server-side=true', '--force-conflicts',
        '--wait', '--timeout', '15m'
    )
    if ($ChartVersion) { $helmArgs += @('--version', $ChartVersion) }
    helm @helmArgs
    if ($LASTEXITCODE -ne 0) { Write-Error 'helm install failed.' }
}

Write-Host "==> Waiting for frontend pod to be Ready"
kubectl -n $Namespace wait --for=condition=Ready pod -l app.kubernetes.io/component=frontend --timeout=300s

# RA-007 / #220: deploy Loki as the Log Correlation logs backend. Runs AFTER the
# otel-demo upgrade above so the image-provider/fraud-detection cuts have already
# freed node memory for Loki's single-binary pod. Idempotent (upgrade --install).
# Pinned chart version so the values schema in infra/loki-values.yaml stays valid.
Write-Host "==> Installing Loki (single-binary) into namespace '$Namespace'"
$lokiValues = Join-Path $repoRoot 'infra/loki-values.yaml'
$lokiArgs = @(
    'upgrade', '--install', 'loki', 'grafana/loki',
    '--namespace', $Namespace,
    '--version', $LokiChartVersion,
    '--values', $lokiValues,
    '--wait', '--timeout', '10m'
)
helm @lokiArgs
# Write-Error is non-terminating in PowerShell; without the explicit exit the
# script would print "Done." with exit 0 even when Loki never came up, and
# observability.logs.query would silently fall back to synthetic. Match the
# fail-fast contract of the otel-demo install guard above.
if ($LASTEXITCODE -ne 0) { Write-Error 'Loki helm install failed.'; exit 1 }

Write-Host "==> Applying the Loki Grafana datasource (Explore access)"
# -n $Namespace so a parameterised install lands the datasource in the SAME
# namespace as Loki + Grafana (the ConfigMap YAML intentionally omits a
# namespace); otherwise Grafana's sidecar never sees it and Explore shows no Loki.
kubectl apply -n $Namespace -f (Join-Path $repoRoot 'infra/loki-grafana-datasource.yaml') | Out-Null

Write-Host ""
Write-Host "==> Done." -ForegroundColor Green
Write-Host ""
Write-Host "Open a separate PowerShell window and run:"
Write-Host "    kubectl -n $Namespace port-forward svc/frontend-proxy 8080:8080"
Write-Host ""
Write-Host "Then browse:"
Write-Host "    Frontend:  http://localhost:8080/"
Write-Host "    Grafana:   http://localhost:8080/grafana/   (admin / admin by default)"
Write-Host "    Jaeger:    http://localhost:8080/jaeger/ui/"
Write-Host "    Loki:      http://localhost:8080/grafana/explore  (pick the Loki datasource)"
Write-Host ""
Write-Host "Trigger a failure:"
Write-Host "  uv run python -m demo.failure_injection.inject --list"
Write-Host "  uv run python -m demo.failure_injection.inject slow-product-catalog"
Write-Host ""
Write-Host "Tear down:  .\infra\teardown.ps1"
