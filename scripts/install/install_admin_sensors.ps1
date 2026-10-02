$ErrorActionPreference = "Stop"

$installDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$hardwareDir = Join-Path $installDir "hardware"
$collector = if (Test-Path "$hardwareDir\sensors\AdminSensorCollector.exe") { "$hardwareDir\sensors\AdminSensorCollector.exe" } else { "$hardwareDir\AdminSensorCollector.exe" }
$snapshot = Join-Path $installDir "data\admin-sensors.json"
$taskName = "PC Control Hardware Sensors"
$identity = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction `
    -Execute $collector `
    -Argument ('"' + $snapshot + '"')
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType Interactive `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Read-only hardware sensors for PC CONTROL" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $taskName
