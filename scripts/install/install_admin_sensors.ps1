$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:LOCALAPPDATA "PCControlAgent"
$hardwareDir = Join-Path $installDir "hardware"
$collector = Join-Path $hardwareDir "AdminSensorCollector.exe"
$snapshot = Join-Path $hardwareDir "admin-sensors.json"
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
