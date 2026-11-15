# run.ps1 - one-command launcher for the load balancer stack.
#
# .\run.ps1           interactive start: menu (algorithm, proxy port, backend
#                     set) -> docker compose up + health wait -> proxy in its
#                     own console window -> verify -> summary.
# .\run.ps1 -Stop     stop the proxy (PID file, fallback: port owner) and
#                     docker compose stop (containers kept on disk).
# .\run.ps1 -Status   container health + proxy listener state.
#
# The script only wraps the exact commands and LB_* env vars already used
# manually; it introduces no new failure modes. Proxy logs live in the
# proxy's own console window (Ctrl+C there stops the proxy).

param(
    [switch]$Stop,
    [switch]$Status
)

# "Continue", not "Stop": PS 5.1 escalates native stderr (docker's progress
# lines go to stderr) into terminating errors under "Stop". The script does
# its own error handling via $LASTEXITCODE and explicit checks.
$ErrorActionPreference = "Continue"
$RepoRoot = $PSScriptRoot
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$DefaultBackends = "backend-1:127.0.0.1:8001,backend-2:127.0.0.1:8002,backend-3:127.0.0.1:8003,backend-4:127.0.0.1:8004"
$DefaultPort = 8080
$PidFile = Join-Path $RepoRoot ".lb-run.pid"
$DockerDesktopExe = "C:\Users\Abdullah\AppData\Local\Programs\DockerDesktop\Docker Desktop.exe"

function Test-DockerDaemon {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Docker daemon not reachable. Start Docker Desktop first:" -ForegroundColor Red
        Write-Host "  $DockerDesktopExe" -ForegroundColor Yellow
        exit 1
    }
}

function Test-PortListening {
    param([int]$Port)
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-PortOwnerPid {
    param([int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) { return $conn.OwningProcess }
    return $null
}

function Show-Menu {
    # Strategy
    Write-Host ""
    Write-Host "Load balancer strategy:"
    Write-Host "  1) round_robin            (default)"
    Write-Host "  2) least_active_requests"
    Write-Host "  3) ip_hash"
    $choice = Read-Host "Choose [1-3, Enter = 1]"
    switch ($choice) {
        "2" { $script:Strategy = "least_active_requests" }
        "3" { $script:Strategy = "ip_hash" }
        default { $script:Strategy = "round_robin" }
    }

    # Proxy port
    $portInput = Read-Host "Proxy listen port [Enter = $DefaultPort]"
    if ($portInput -match '^\d+$') { $script:ProxyPort = [int]$portInput } else { $script:ProxyPort = $DefaultPort }

    # Backend set
    Write-Host ""
    Write-Host "Backend set:"
    Write-Host "  Enter) full 4-backend farm (backend-1..4 on 127.0.0.1:8001-8004)"
    Write-Host "  c)     custom  id:host:port,id:host:port,..."
    $beInput = Read-Host "Choose"
    if ($beInput -eq "c") {
        while ($true) {
            $custom = Read-Host "Backend string (id:host:port,...)"
            if ([string]::IsNullOrWhiteSpace($custom)) {
                Write-Host "  empty input - enter entries like b1:127.0.0.1:9001" -ForegroundColor Red
                continue
            }
            $parts = $custom.Split(",") | Where-Object { $_.Trim() -ne "" }
            $valid = $true
            foreach ($p in $parts) {
                if ($p.Split(":").Count -ne 3) { $valid = $false; break }
            }
            if ($valid -and $parts.Count -gt 0) { $script:Backends = ($parts | ForEach-Object { $_.Trim() }) -join ","; break }
            Write-Host "  invalid: every entry needs exactly 3 ':' parts (id:host:port)" -ForegroundColor Red
        }
    }
    else {
        $script:Backends = $DefaultBackends
    }
}

function Wait-FarmHealthy {
    param([int]$TimeoutSeconds = 30)
    Write-Host "Waiting for backend containers to become healthy..."
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $out = docker compose ps --format "{{.Name}} {{.Status}}" 2>$null
        $unhealthy = $out | Where-Object { $_ -match "backend" -and $_ -notmatch "\(healthy\)" }
        $running = $out | Where-Object { $_ -match "backend" }
        if ($running.Count -gt 0 -and $unhealthy.Count -eq 0) {
            Write-Host "  all backend containers healthy" -ForegroundColor Green
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Host "  backends did not become healthy within ${TimeoutSeconds}s:" -ForegroundColor Red
    $out | ForEach-Object { Write-Host "  $_" }
    return $false
}

function Start-Proxy {
    $env:LB_PROXY_HOST = "127.0.0.1"
    $env:LB_PROXY_PORT = "$script:ProxyPort"
    $env:LB_BACKENDS = $script:Backends
    $env:LB_STRATEGY = $script:Strategy

    $proc = Start-Process -FilePath $VenvPython `
        -ArgumentList "-m", "load_balancer.main" `
        -WorkingDirectory $RepoRoot `
        -PassThru -NoNewWindow:$false
    Set-Content -Path $PidFile -Value $proc.Id

    # verify the proxy answers before declaring success
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$script:ProxyPort/" -UseBasicParsing -TimeoutSec 2
            if ($resp.StatusCode -eq 200) { return $true }
        }
        catch {
            if (-not $proc.HasExited) { Start-Sleep -Milliseconds 500 } else { break }
        }
    }
    return $false
}

if ($Status) {
    Test-DockerDaemon
    Write-Host "== Docker backend farm =="
    docker compose ps --format "{{.Name}}  {{.Status}}"
    $port = $DefaultPort
    if (Test-Path $PidFile) {
        $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
            Write-Host "== Proxy == listening (pid $savedPid)"
            exit 0
        }
    }
    if (Test-PortListening -Port $port) {
        Write-Host "== Proxy == listening on :$port (pid $(Get-PortOwnerPid -Port $port), not started by run.ps1)"
    }
    else {
        Write-Host "== Proxy == not running"
    }
    exit 0
}

if ($Stop) {
    if (Test-Path $PidFile) {
        $savedPid = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $savedPid -Force
            Write-Host "Proxy stopped (pid $savedPid)"
        }
        else {
            Write-Host "Proxy not running (stale pid file)"
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    elseif (Test-PortListening -Port $DefaultPort) {
        $owner = Get-PortOwnerPid -Port $DefaultPort
        Stop-Process -Id $owner -Force
        Write-Host "Proxy stopped (port owner pid $owner)"
    }
    else {
        Write-Host "Proxy not running"
    }
    Test-DockerDaemon
    docker compose stop | Out-Null
    Write-Host "Backend farm stopped (containers kept; docker compose up -d restarts them)"
    exit 0
}

# ---- default: interactive start ----
Test-DockerDaemon
Show-Menu

if (Test-PortListening -Port $script:ProxyPort) {
    Write-Host "Port $($script:ProxyPort) is already in use (pid $(Get-PortOwnerPid -Port $script:ProxyPort)) - refusing to double-start." -ForegroundColor Red
    Write-Host "Stop the existing proxy first: .\run.ps1 -Stop"
    exit 1
}

Test-DockerDaemon
docker compose up -d | Out-Null
if (-not (Wait-FarmHealthy)) { exit 1 }

Write-Host "Starting proxy: strategy=$($script:Strategy) port=$($script:ProxyPort)"
if (Start-Proxy) {
    Write-Host ""
    Write-Host "=== load balancer running ===" -ForegroundColor Green
    Write-Host "  strategy: $($script:Strategy)"
    Write-Host "  proxy:    http://127.0.0.1:$($script:ProxyPort)/  (pid $((Get-Content $PidFile)))"
    Write-Host "  try:      http://127.0.0.1:$($script:ProxyPort)/"
    Write-Host "            http://127.0.0.1:$($script:ProxyPort)/work?delay_ms=100"
    Write-Host "  proxy log: in its own console window (Ctrl+C there stops it)"
    Write-Host "  stop all:  .\run.ps1 -Stop"
    exit 0
}
else {
    Write-Host "Proxy failed to start - check its console window." -ForegroundColor Red
    exit 1
}
