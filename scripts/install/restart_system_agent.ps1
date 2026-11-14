$ErrorActionPreference = "Stop"
$installDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$logDir = Join-Path $installDir "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir "restart_system_agent.log"
function Log-Msg($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

Log-Msg "=== Starting PC Control System & Desktop Agent restart ==="

Log-Msg "Stopping scheduled tasks..."
Stop-ScheduledTask -TaskName "PC Control System Agent" -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "PC Control Desktop Agent" -ErrorAction SilentlyContinue

Log-Msg "Finding python processes running PC Control agents inside $installDir..."
$escapedDir = [Regex]::Escape($installDir)
$agentPattern = "$escapedDir[\\/]+(boot_agent|agent)\.py"
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("python.exe", "pythonw.exe")) -and
    $_.CommandLine -and
    ($_.CommandLine -match $agentPattern)
}

$failedKills = @()
foreach ($p in $procs) {
    Log-Msg "Killing process $($p.ProcessId) ($($p.Name)) - $($p.CommandLine)"
    & taskkill.exe /F /PID $p.ProcessId /T 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Log-Msg "WARNING: taskkill failed for PID $($p.ProcessId) with exit code $LASTEXITCODE"
        $failedKills += $p.ProcessId
    }
}

Start-Sleep -Seconds 3

Log-Msg "Starting PC Control System Agent..."
Start-ScheduledTask -TaskName "PC Control System Agent" -ErrorAction Stop

Start-Sleep -Seconds 2

Log-Msg "Starting PC Control Desktop Agent..."
Start-ScheduledTask -TaskName "PC Control Desktop Agent" -ErrorAction Stop

Start-Sleep -Seconds 1
foreach ($taskName in @("PC Control System Agent", "PC Control Desktop Agent")) {
    $state = (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue).State
    Log-Msg "Task '$taskName' state: $state"
    if ($state -ne "Running") {
        throw "Task '$taskName' did not enter Running state (actual: $state)"
    }
}

if ($failedKills.Count -gt 0) {
    throw "Some old agent processes failed to terminate: $($failedKills -join ', ')"
}

Log-Msg "=== Restart completed successfully ==="

