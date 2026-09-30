# Windows Task Scheduler registration for PCControl Weekly Code Backup
# Dynamically resolves project paths without hardcoding user or drive roots

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$ProjectDir = (Get-Item "$ScriptDir\..").FullName
$BackupScript = Join-Path $ScriptDir "auto_backup.py"

# Find pythonw or python
$PythonExe = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $PythonExe) {
    $PythonExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}

if (-not $PythonExe) {
    Write-Error "Python executable not found in PATH."
    exit 1
}

$TaskName = "PC Control Weekly Code Backup"
$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$BackupScript`" --scheduled" -WorkingDirectory $ProjectDir

# Trigger: Every Sunday at 04:00 AM
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 04:00AM

# Settings: run if missed, allow on battery, limit 15 mins
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

# Register task under current user
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Weekly source code backup for PCControlAgent (PC, ESP8266, and VDS)." | Out-Null
    Write-Host "[OK] Scheduled task '$TaskName' registered successfully!" -ForegroundColor Green
    Write-Host "    Trigger: Weekly on Sunday at 04:00 AM (Runs when available if missed)"
    Write-Host "    Action : $PythonExe `"$BackupScript`" --scheduled"
    Write-Host "    WorkDir: $ProjectDir"
} catch {
    Write-Error ("Failed to register scheduled task: " + $_.Exception.Message)
    exit 1
}
