# Restart the local server: stop whatever is listening on the configured port (or the PID in
# .server.pid), start a fresh hidden instance, then poll /api/health. Chatty on purpose.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "no .venv - run .\run.ps1 first" }
$pidFile = Join-Path $root ".server.pid"

$port = 8000
$cfg = Join-Path $root "config.json"
if (Test-Path -LiteralPath $cfg) {
    try { $port = [int]((Get-Content -LiteralPath $cfg -Raw | ConvertFrom-Json).port) } catch {}
}

Write-Host "stopping old server..."
$killed = @()
if (Test-Path -LiteralPath $pidFile) {
    [int]$old = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$old)) { $killed += $old }
    Remove-Item -LiteralPath $pidFile -Force
}
$killed += Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
foreach ($procId in ($killed | Where-Object { $_ -and $_ -ne $PID } | Select-Object -Unique)) {
    try {
        Stop-Process -Id $procId -Force -ErrorAction Stop
        Write-Host "  stopped pid $procId"
    } catch { Write-Host "  pid $procId already gone" }
}
if ($killed.Count -eq 0) { Write-Host "  nothing was listening on $port" }

$log = Join-Path $env:TEMP "yt-mp3-server.err.log"
# Start-Process would hand the new server a copy of our own stdout/stderr pipe handles, which
# keeps a calling terminal (or an automation harness) waiting for output that never ends.
# Win32_Process Create starts it fully detached; cmd /c keeps stdout/stderr redirected to a log.
$cmdline = "`"$env:ComSpec`" /c `"`"$python`" app.py > `"$log`" 2>&1`""
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine      = $cmdline
    CurrentDirectory = $root
}
if ($created.ReturnValue -ne 0 -or -not $created.ProcessId) {
    throw "failed to start server (Win32_Process Create returned $($created.ReturnValue))"
}
Write-Host "started pid $($created.ProcessId) (cmd wrapper), waiting for http://127.0.0.1:$port ..."

for ($try = 0; $try -lt 40; $try++) {
    Start-Sleep -Milliseconds 250
    try {
        $health = Invoke-WebRequest "http://127.0.0.1:$port/api/health" -UseBasicParsing -TimeoutSec 2
        # remember the real python pid (the listener), not the cmd wrapper
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            Set-Content -LiteralPath $pidFile
        Write-Host $health.Content
        exit 0
    } catch {}
    if ($try % 8 -eq 7) { Write-Host "  still waiting..." }
}
if (Test-Path -LiteralPath $log) { Write-Host "server did not answer on $port - last log lines:"; Get-Content -LiteralPath $log -Tail 20 }
exit 1
