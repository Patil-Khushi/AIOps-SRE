# Bring the demo back to a known-clean baseline.
#
#   .\reset.ps1              # fault scenarios off, chatops outbox truncated
#   .\reset.ps1 -Hard        # also wipe verdicts / classifications / tickets from state.db
#   .\reset.ps1 -Data        # also wipe the SUT's own data (orders/users/payments)
#   .\reset.ps1 -Telemetry   # also wipe metric/log/trace HISTORY   (implies -Data)
#   .\reset.ps1 -All         # everything
#   .\reset.ps1 -Data -KeepPaused   # empty the tables and KEEP them empty above
#
# The switches are layered by what they clean and what they cost:
#
#   (none)      Agent inputs. Flags off, audit log truncated. Seconds, no cluster
#               contact.
#   -Hard       Agent OUTPUTS — state.db verdicts/classifications/tickets. The
#               dashboard's "AI Reasoning" and history views go empty.
#   -Data       The application's own state: Postgres `orders`, MySQL `users`,
#               Redis payments. Also rolling-restarts the three services,
#               because Prometheus counters like orders_created_total live in
#               the process — truncating the table does NOT move them, and
#               without the restart the dashboard reports hundreds of orders
#               against an empty table.
#   -Telemetry  Metric/log/trace history itself, so the dashboard GRAPHS start
#               empty rather than showing the old traffic behind you. This is
#               the one you want before recording a demo. Costs ~3 min because
#               it deletes and re-provisions PVCs.
#
# NOTE: the default and -Hard still do NOT touch the cluster. -Data and
# -Telemetry DO — they bounce pods and (for -Telemetry) delete PVCs. They do not
# kill start.ps1's port-forwards, but a port-forward to a pod that gets replaced
# will drop; re-run start.ps1 if a UI stops responding after -Telemetry.
#
# Note on the post-reset summary's "prom active alerts" line: Prometheus alert
# rules use rolling [2m] windows. An alert that was firing from a prior fault
# stays firing for ~2 min after you flip the flag off — that's lag, not a reset
# failure. The "scenarios" / "verdicts" lines are the source of truth for
# whether reset actually worked.

[CmdletBinding()]
param(
    [switch]$Hard,
    [switch]$Data,
    [switch]$Telemetry,
    [switch]$All,
    # Leave the load generator scaled to 0 after a -Data reset, so the tables
    # STAY empty. Without this the generator resumes and writes an order every
    # ~5s, so "0 rows" is true for about five seconds — fine when you want a
    # clean baseline with live traffic, useless when you want to show an empty
    # database or hand-drive the app yourself.
    [switch]$KeepPaused,
    [string]$UiBase = 'http://localhost:8765',
    [string]$AppNamespace = 'ecommerce',
    [string]$ObsNamespace = 'observability',
    [string]$LokiNamespace = 'otel-demo'
)

if ($All) { $Hard = $true; $Data = $true; $Telemetry = $true }
# Wiping the metric history while leaving 700 orders in Postgres produces a
# dashboard that disagrees with itself: empty graphs, full tables. Telemetry
# implies data.
if ($Telemetry) { $Data = $true }

$ErrorActionPreference = 'Continue'
$repo = $PSScriptRoot
$problems = @()

function _step($label, [scriptblock]$action) {
    Write-Host ("[reset] {0,-40} " -f $label) -NoNewline
    try { & $action; Write-Host "ok" -ForegroundColor Green }
    catch { Write-Host "FAIL: $($_.Exception.Message)" -ForegroundColor Red; $script:problems += $label }
}

# 1. Flip every UI-known scenario back to off.
#
# This is the ONLY fault-clearing step now. There used to be a second
# "inject.py --clear" belt-and-suspenders call against flagd, the OpenTelemetry
# demo's feature-flag service. Both halves of it are gone: flagd is not deployed
# anywhere in the cluster, and `demo/failure_injection/` moved to
# `demo/ecommerce/failure_injection/` with an env-var-patching implementation
# that has no flags at all. The call raised ModuleNotFoundError on every run and
# still printed "ok", because `2>&1 | Out-Null` swallowed the error and
# $ErrorActionPreference='Continue' kept the nonzero exit from throwing — a
# reset step that reported success while doing nothing.
_step "POST /api/scenarios/reset-all" {
    $r = Invoke-RestMethod -Method POST "$UiBase/api/scenarios/reset-all" -TimeoutSec 15
    Write-Host -NoNewline ("(reset_count={0}) " -f $r.reset_count)
}

# 2. Truncate the chatops audit log.
_step "truncate demo/audit/chatops.jsonl" {
    $p = Join-Path $repo 'demo\audit\chatops.jsonl'
    if (Test-Path $p) { Set-Content -Path $p -Value '' -Encoding utf8 -NoNewline }
}

# 4. Clear scratch eval-harness output files.
_step "remove .tmp_eval*.txt" {
    Get-ChildItem -Path $repo -Filter '.tmp_eval*.txt' -ErrorAction SilentlyContinue | Remove-Item -Force
}

# 5. Hard reset: wipe persisted agent state (verdicts, classifications, tickets,
#    notifications). Dashboard's "AI Reasoning" / history views become empty.
if ($Hard) {
    _step "DELETE FROM verdicts/* (state.db)" {
        $db = Join-Path $repo 'data\state.db'
        if (-not (Test-Path $db)) { throw "no DB at $db" }
        $py = @"
import sqlite3
con = sqlite3.connect(r'$db')
cur = con.cursor()
# delete child rows first to satisfy foreign keys
for t in ('notifications','tickets','classifications','verdicts'):
    try:
        cur.execute(f'DELETE FROM {t}')
    except sqlite3.OperationalError:
        pass
con.commit()
con.close()
print('hard-reset ok')
"@
        $py | uv run --quiet python - | Out-Null
    }
}

# 6. Data reset: the SUT's own datastores + the in-process metric counters.
#
# Rancher Desktop's kuberlr-wrapped kubectl rejects some flags when invoked
# non-interactively, so prefer the standalone binary (same rule as start.ps1).
#
# Every kubectl call below invokes $kubectl DIRECTLY rather than via a helper
# function: PowerShell strips the `--` end-of-parameters token when binding to a
# function, so `kubectl exec pod -- psql -U appuser` would arrive as
# `... psql -U appuser` and kubectl would reject `-U` as one of its own flags.
$kubectl = Join-Path $env:LOCALAPPDATA 'Programs\kubectl\kubectl.exe'
if (-not (Test-Path $kubectl)) { $kubectl = 'kubectl' }

$loadgenPaused = $false

if ($Data) {
    # Pause the load generator FIRST. It writes an order every few seconds, so
    # racing it means the "clean" tables have rows before the script returns.
    _step "pause loadgen" {
        $exists = [bool](& $kubectl get deploy loadgen -n $AppNamespace --ignore-not-found -o name)
        if ($exists) {
            & $kubectl scale deploy/loadgen -n $AppNamespace --replicas=0 | Out-Null
            & $kubectl wait --for=delete pod -l app=loadgen -n $AppNamespace --timeout=60s 2>&1 | Out-Null
            $script:loadgenPaused = $true
            Write-Host -NoNewline '(paused) '
        }
        else { Write-Host -NoNewline '(absent) ' }
    }

    # RESTART IDENTITY so ids start at 1 again — a demo whose first order is
    # #721 invites "where are the other 720?". Services re-create their schema
    # on boot, so dropping rows rather than tables is enough.
    _step "TRUNCATE postgres orders" {
        & $kubectl exec -n $AppNamespace postgres-0 -- psql -U appuser -d orders -c 'TRUNCATE TABLE orders RESTART IDENTITY;' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'psql returned nonzero' }
    }

    # MYSQL_PWD, not -p<password>: the -p form prints "Using a password on the
    # command line interface can be insecure" to stderr, and PowerShell wraps
    # native stderr in a NativeCommandError that reads as a failed step.
    _step "TRUNCATE mysql users" {
        & $kubectl exec -n $AppNamespace mysql-0 -- sh -c "MYSQL_PWD=rootpass mysql -uroot -D users -e 'TRUNCATE TABLE users;'" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'mysql returned nonzero' }
    }

    _step "FLUSHDB redis payments" {
        & $kubectl exec -n $AppNamespace redis-0 -- redis-cli FLUSHDB | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'redis-cli returned nonzero' }
    }
}

# 7. Telemetry reset: the history behind the dashboard graphs.
if ($Telemetry) {
    # Prometheus' delete_series admin API is gated behind --web.enable-admin-api,
    # which this chart does not pass (it passes --web.enable-lifecycle only), so
    # deleting the volume is the supported route without changing pod flags.
    _step "wipe prometheus TSDB" {
        & $kubectl scale deploy/prometheus-server -n $ObsNamespace --replicas=0 | Out-Null
        & $kubectl wait --for=delete pod -l app.kubernetes.io/name=prometheus -n $ObsNamespace --timeout=120s 2>&1 | Out-Null
        & $kubectl delete pvc prometheus-server -n $ObsNamespace --ignore-not-found | Out-Null

        # RECREATE the PVC before scaling back up. prometheus-server is a
        # Deployment, so its volume is a standalone Helm-managed PVC — NOT a
        # StatefulSet volumeClaimTemplate. Nothing recreates it on scale-up, and
        # the pod sits in Pending forever with
        #   FailedScheduling: persistentvolumeclaim "prometheus-server" not found
        # which reads like a scheduling problem rather than a missing volume.
        # (Loki below is a StatefulSet, so its PVC *is* templated and comes back
        # by itself — the two are deliberately handled differently.)
        $manifest = helm get manifest prometheus --namespace $ObsNamespace 2>$null
        $pvcDoc = ($manifest -join "`n") -split "(?m)^---\s*$" | Where-Object { $_ -match 'kind:\s*PersistentVolumeClaim' }
        if (-not $pvcDoc) { throw 'could not find the PVC in the prometheus helm manifest' }
        $pvcDoc | & $kubectl apply -f - | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'PVC recreate failed' }

        & $kubectl scale deploy/prometheus-server -n $ObsNamespace --replicas=1 | Out-Null
    }

    _step "wipe loki chunks" {
        $exists = [bool](& $kubectl get statefulset loki -n $LokiNamespace --ignore-not-found -o name)
        if (-not $exists) { Write-Host -NoNewline '(absent) '; return }
        # The PVC is bound to the pod ordinal, so it has to go while the pod is
        # down or the delete hangs in Terminating.
        & $kubectl scale statefulset/loki -n $LokiNamespace --replicas=0 | Out-Null
        & $kubectl wait --for=delete pod/loki-0 -n $LokiNamespace --timeout=120s 2>&1 | Out-Null
        & $kubectl delete pvc -l app.kubernetes.io/name=loki -n $LokiNamespace --ignore-not-found | Out-Null
        & $kubectl scale statefulset/loki -n $LokiNamespace --replicas=1 | Out-Null
    }

    # Jaeger allInOne keeps spans in memory, so a restart IS the wipe.
    _step "restart jaeger (in-memory spans)" {
        & $kubectl rollout restart deploy/jaeger -n $ObsNamespace | Out-Null
    }
}

# 8. Zero the in-process Prometheus counters, then let the generator run again.
if ($Data) {
    _step "restart services (zero counters)" {
        foreach ($svc in @('user-service', 'order-service', 'payment-service')) {
            & $kubectl rollout restart deploy/$svc -n $AppNamespace | Out-Null
        }
        foreach ($svc in @('user-service', 'order-service', 'payment-service')) {
            & $kubectl rollout status deploy/$svc -n $AppNamespace --timeout=180s | Out-Null
        }
    }

    if ($loadgenPaused -and -not $KeepPaused) {
        _step "resume loadgen" {
            & $kubectl scale deploy/loadgen -n $AppNamespace --replicas=1 | Out-Null
        }
    }
    elseif ($loadgenPaused) {
        Write-Host ("[reset] {0,-40} (left at 0 replicas)" -f 'loadgen NOT resumed') -ForegroundColor DarkGray
    }
}

# 9. Final state summary.
Write-Host ''
Write-Host '=== post-reset state ==='
try {
    # /api/scenarios returns {"scenarios":[...]}, not a bare array. Piping the
    # response straight into Where-Object filtered the single WRAPPER object,
    # which has no `current_variant`, so `-ne 'off'` was always true and this
    # line reported "scenarios still on:" with an empty id list on every run —
    # even immediately after reset-all cleared all 12.
    # NOT $all - that is case-insensitively the same variable as the -All switch
    # parameter, which is typed [switch], so assigning an array to it throws
    # "Cannot convert System.Object[] to SwitchParameter".
    $known = (Invoke-RestMethod "$UiBase/api/scenarios" -TimeoutSec 5).scenarios
    $on = @($known | Where-Object { $_.current_variant -ne 'off' })
    if ($on.Count) { Write-Host "  scenarios still on: $(($on.scenario_id) -join ', ')" -ForegroundColor Yellow }
    else { Write-Host ("  scenarios:        all off ({0} checked)" -f @($known).Count) -ForegroundColor Green }
} catch { Write-Host "  scenarios:        UNKNOWN ($($_.Exception.Message))" -ForegroundColor Yellow }

try {
    $alerts = (Invoke-RestMethod 'http://localhost:9090/api/v1/alerts' -TimeoutSec 5).data.alerts
    if ($alerts.Count -eq 0) {
        Write-Host "  prom active alerts: 0" -ForegroundColor Green
    } else {
        $names = ($alerts | ForEach-Object { $_.labels.alertname }) -join ', '
        Write-Host ("  prom active alerts: {0} ({1}) -- lag from prior fault, clears in ~2 min" -f $alerts.Count, $names) -ForegroundColor DarkGray
    }
} catch { Write-Host "  prom alerts:      UNKNOWN" -ForegroundColor Yellow }

try {
    $v = Invoke-RestMethod "$UiBase/api/verdicts?limit=1" -TimeoutSec 5
    Write-Host ("  persisted verdicts: {0}" -f $v.count)
} catch { Write-Host "  verdicts:         UNKNOWN" -ForegroundColor Yellow }

if ($Data) {
    # Read straight from the datastores rather than through Grafana: this has to
    # answer "did the reset land", and a Grafana panel can read empty for
    # reasons that have nothing to do with the data (see the postgres datasource
    # plugin-id incident in infra/observability/grafana-values.yaml).
    $orders = (& $kubectl exec -n $AppNamespace postgres-0 -- psql -U appuser -d orders -t -c 'SELECT COUNT(*) FROM orders;' | Out-String).Trim()
    $users = (& $kubectl exec -n $AppNamespace mysql-0 -- sh -c "MYSQL_PWD=rootpass mysql -uroot -D users -s -N -e 'SELECT COUNT(*) FROM users;'" | Out-String).Trim()
    $payments = (& $kubectl exec -n $AppNamespace redis-0 -- redis-cli DBSIZE | Out-String).Trim()
    Write-Host ("  postgres orders:    {0}" -f $orders)
    Write-Host ("  mysql users:        {0}" -f $users)
    Write-Host ("  redis payments:     {0}" -f $payments)
    if ($loadgenPaused -and -not $KeepPaused) {
        Write-Host '  loadgen:            running again - counts climb within seconds' -ForegroundColor DarkGray
        Write-Host ("                      stop it: kubectl -n {0} scale deploy/loadgen --replicas=0" -f $AppNamespace) -ForegroundColor DarkGray
    }
    elseif ($loadgenPaused) {
        Write-Host '  loadgen:            PAUSED - tables stay empty until you resume it' -ForegroundColor Yellow
        Write-Host ("                      resume: kubectl -n {0} scale deploy/loadgen --replicas=1" -f $AppNamespace) -ForegroundColor DarkGray
        Write-Host '                      NOTE: with no traffic the dashboard metric panels go' -ForegroundColor DarkGray
        Write-Host '                      flat within ~5 min (every panel is a rate() over 5m).' -ForegroundColor DarkGray
    }
}

if ($Data -and -not $Telemetry) {
    Write-Host ''
    Write-Host '  NOTE: metric/log/trace HISTORY was kept, so the dashboard graphs still' -ForegroundColor DarkGray
    Write-Host '        show the old traffic. Use -Telemetry to clear those too.' -ForegroundColor DarkGray
}

Write-Host ''
if ($problems.Count -eq 0) { Write-Host 'CLEAN' -ForegroundColor Green }
else { Write-Host ('DIRTY  ({0} failed)' -f $problems.Count) -ForegroundColor Red; exit 1 }
