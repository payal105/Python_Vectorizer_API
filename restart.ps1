<#
.SYNOPSIS
    Stop any running Python Vector API server and start a fresh one.

.DESCRIPTION
    Uvicorn's auto-reloader runs the app in a child process spawned through
    multiprocessing. That child's command line reads "spawn_main(parent_pid=...)"
    with no mention of run.py or uvicorn, so closing the terminal instead of
    pressing Ctrl+C orphans it. The orphan keeps holding port 8000 and keeps
    serving the old code, which looks exactly like "my changes did nothing".

    This script clears those strays, then starts the server in the foreground
    so Ctrl+C stops it cleanly.

.EXAMPLE
    .\restart.ps1
    .\restart.ps1 -Port 8080
#>
[CmdletBinding()]
param(
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Host "No virtualenv found at .venv" -ForegroundColor Red
    Write-Host "Create it with:  py -m venv .venv" -ForegroundColor Yellow
    Write-Host "Then:            .venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# --- Collect stale processes -------------------------------------------------
$doomed = New-Object System.Collections.Generic.HashSet[int]

# 1. Whatever currently holds the port.
try {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
        ForEach-Object { [void]$doomed.Add([int]$_.OwningProcess) }
} catch {
    # No listener, or the cmdlet is unavailable on this host. Nothing to do.
}

$pythonProcesses = @(
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue
)

# 2. Servers we can identify outright by their command line.
$pythonProcesses |
    Where-Object { $_.CommandLine -like '*run.py*' -or $_.CommandLine -like '*uvicorn*' } |
    ForEach-Object { [void]$doomed.Add([int]$_.ProcessId) }

# 3. Orphaned reload workers: a multiprocessing child whose parent is gone.
#    Checking the parent keeps this from killing unrelated multiprocessing
#    jobs that are still healthy.
foreach ($proc in $pythonProcesses) {
    if ($proc.CommandLine -notlike '*multiprocessing*') { continue }
    $parentAlive = $null -ne (Get-Process -Id $proc.ParentProcessId -ErrorAction SilentlyContinue)
    if (-not $parentAlive) { [void]$doomed.Add([int]$proc.ProcessId) }
}

# --- Stop them ---------------------------------------------------------------
$stopped = 0
foreach ($processId in $doomed) {
    $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($proc) {
        Write-Host "  stopping stale process $processId ($($proc.ProcessName))" -ForegroundColor DarkYellow
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        $stopped++
    }
}

if ($stopped -gt 0) {
    Start-Sleep -Seconds 2
    Write-Host "cleared $stopped stale process(es)" -ForegroundColor Yellow
} else {
    Write-Host "nothing stale to clear" -ForegroundColor DarkGray
}

# --- Start -------------------------------------------------------------------
Write-Host ""
Write-Host "Python Vector API  ->  http://127.0.0.1:$Port/docs" -ForegroundColor Green
Write-Host "Credentials: demo_id / demo_secret   (Ctrl+C to stop)" -ForegroundColor DarkGray
Write-Host "After a code change, hard-refresh the docs page with Ctrl+Shift+R." -ForegroundColor DarkGray
Write-Host ""

$env:VECTOR_PORT = $Port
& $python run.py
