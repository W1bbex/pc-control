$logFile = Join-Path $PSScriptRoot "..\..\logs\restart_system_agent.log"
function Log-Msg($msg) {
    $line = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

Log-Msg "=== Starting PC Control System & Desktop Agent restart ==="

Log-Msg "Stopping scheduled tasks..."
Stop-ScheduledTask -TaskName "PC Control System Agent" -ErrorAction SilentlyContinue
Stop-ScheduledTask -TaskName "PC Control Desktop Agent" -ErrorAction SilentlyContinue

Log-Msg "Finding all python processes running PC Control agents..."
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("python.exe", "pythonw.exe")) -and (
        $_.CommandLine -like "*boot_agent.py*" -or
        $_.CommandLine -like "*agent.py*" -or
        $_.CommandLine -like "*PCControlAgent*" -or
        $_.CommandLine -like "*pc-control*"
    )
}

foreach ($p in $procs) {
    Log-Msg "Killing process $($p.ProcessId) ($($p.Name)) - $($p.CommandLine)"
    & cmd.exe /c "taskkill /F /PID $($p.ProcessId) /T 2>&1" | Out-Null
}

Start-Sleep -Seconds 3

Log-Msg "Starting PC Control System Agent..."
Start-ScheduledTask -TaskName "PC Control System Agent" -ErrorAction Continue

Start-Sleep -Seconds 2

Log-Msg "Starting PC Control Desktop Agent..."
Start-ScheduledTask -TaskName "PC Control Desktop Agent" -ErrorAction Continue

Log-Msg "=== Restart completed successfully ==="

