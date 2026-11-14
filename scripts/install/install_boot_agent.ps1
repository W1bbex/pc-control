$ErrorActionPreference = "Stop"

$installDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$hardwareDir = Join-Path $installDir "hardware"
$python = Join-Path $installDir ".venv\Scripts\pythonw.exe"
$desktopAgent = Join-Path $installDir "agent.py"
$systemAgent = Join-Path $installDir "boot_agent.py"
$collector = if (Test-Path "$hardwareDir\sensors\AdminSensorCollector.exe") { "$hardwareDir\sensors\AdminSensorCollector.exe" } else { "$hardwareDir\AdminSensorCollector.exe" }
$snapshot = Join-Path $installDir "data\admin-sensors.json"
$logPath = Join-Path $installDir "logs\boot-install.log"

New-Item -ItemType Directory -Force -Path (Split-Path $logPath) | Out-Null

if (!(Test-Path -LiteralPath $python)) {
    throw "Python runtime not found: $python"
}
if (!(Test-Path -LiteralPath $systemAgent)) {
    throw "System agent not found: $systemAgent"
}
if (!(Test-Path -LiteralPath $desktopAgent)) {
    throw "Desktop agent not found: $desktopAgent"
}
if (!(Test-Path -LiteralPath $collector)) {
    throw "Sensor collector not found: $collector"
}

# Files created by the SYSTEM collector must remain readable by the desktop
# agent. Resolve an interactive desktop user reliably even under SSH/WinRM.
$consoleUser = (Get-CimInstance Win32_ComputerSystem).UserName
if (-not $consoleUser) {
    $explorerProc = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | Select-Object -First 1
    if ($explorerProc) {
        $owner = Invoke-CimMethod -InputObject $explorerProc -MethodName GetOwner
        if ($owner -and $owner.User) {
            $consoleUser = if ($owner.Domain) { "$($owner.Domain)\$($owner.User)" } else { $owner.User }
        }
    }
}
$currentUser = if ($consoleUser) { $consoleUser } else { $null }
if (-not $currentUser -or $currentUser -eq "NT AUTHORITY\SYSTEM") {
    throw "No interactive desktop user found. Run installer with an active user session or pass an explicit user."
}
$currentUserSid = (New-Object System.Security.Principal.NTAccount($currentUser)).Translate([System.Security.Principal.SecurityIdentifier]).Value
$snapshotDir = Split-Path -Parent $snapshot
if (-not (Test-Path -LiteralPath $snapshotDir)) {
    New-Item -ItemType Directory -Force -Path $snapshotDir | Out-Null
}
foreach ($aclPath in @($hardwareDir, $snapshotDir)) {
    & icacls.exe $aclPath /grant "*$($currentUserSid):(OI)(CI)(RX)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not grant desktop-agent read access to: $aclPath"
    }
}

$startup = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -Priority 1

foreach ($taskName in @(
    "PC Control System Agent",
    "PC Control Desktop Agent",
    "PC Control Hardware Sensors"
)) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    }
}
Start-Sleep -Seconds 2

$agentAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument ('"' + $systemAgent + '"') `
    -WorkingDirectory $installDir

Register-ScheduledTask `
    -TaskName "PC Control System Agent" `
    -Action $agentAction `
    -Trigger $startup `
    -Principal $principal `
    -Settings $settings `
    -Description "PC CONTROL MQTT core available before Windows logon" `
    -Force | Out-Null

$desktopTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$desktopPrincipal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Highest
$desktopAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument ('"' + $desktopAgent + '"') `
    -WorkingDirectory $installDir

Register-ScheduledTask `
    -TaskName "PC Control Desktop Agent" `
    -Action $desktopAction `
    -Trigger $desktopTrigger `
    -Principal $desktopPrincipal `
    -Settings $settings `
    -Description "PC CONTROL desktop control after user logon" `
    -Force | Out-Null

$sensorAction = New-ScheduledTaskAction `
    -Execute $collector `
    -Argument ('"' + $snapshot + '"') `
    -WorkingDirectory $hardwareDir

Register-ScheduledTask `
    -TaskName "PC Control Hardware Sensors" `
    -Action $sensorAction `
    -Trigger $startup `
    -Principal $principal `
    -Settings $settings `
    -Description "Read-only PC CONTROL hardware sensors before logon" `
    -Force | Out-Null

Start-ScheduledTask -TaskName "PC Control Hardware Sensors"
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName "PC Control System Agent"
Start-ScheduledTask -TaskName "PC Control Desktop Agent"

$legacyShortcut = Join-Path ([Environment]::GetFolderPath("Startup")) "PC Control Agent.lnk"
if (Test-Path -LiteralPath $legacyShortcut) {
    Remove-Item -LiteralPath $legacyShortcut -Force
}

@(
    "installed=$(Get-Date -Format o)"
    "agent_task=PC Control System Agent"
    "desktop_task=PC Control Desktop Agent"
    "sensor_task=PC Control Hardware Sensors"
    "principal=SYSTEM"
    "trigger=AtStartup"
) | Set-Content -LiteralPath $logPath -Encoding UTF8
