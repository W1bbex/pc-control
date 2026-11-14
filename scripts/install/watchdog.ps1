$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $scriptDir "..\..\agent.py")) {
    $installDir = (Resolve-Path "$scriptDir\..\..").Path
} else {
    $installDir = $scriptDir
}

$logDir = Join-Path $installDir "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir "watchdog_desktop.log"

$python = Join-Path $installDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    $python = "pythonw.exe"
}
$agent = Join-Path $installDir "agent.py"

function Start-Agent($script) {
    return Start-Process `
        -FilePath $python `
        -ArgumentList @('"' + $script + '"') `
        -WorkingDirectory $installDir `
        -PassThru
}

$backoff = 5
$desktopProcess = $null

while ($true) {
    try {
        if (-not $desktopProcess -or $desktopProcess.HasExited) {
            $startedAt = Get-Date
            $desktopProcess = Start-Agent $agent
            if (-not $desktopProcess) {
                throw "Start-Process returned null for $agent"
            }
            $backoff = 5
        }
    } catch {
        Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERROR: $_" -Encoding utf8
        Start-Sleep -Seconds $backoff
        $backoff = [Math]::Min(60, $backoff * 2)
        continue
    }
    Start-Sleep -Seconds 5
    if ($desktopProcess -and $desktopProcess.HasExited) {
        $uptime = ((Get-Date) - $startedAt).TotalSeconds
        if ($uptime -lt 15) {
            Start-Sleep -Seconds $backoff
            $backoff = [Math]::Min(60, $backoff * 2)
        } else {
            $backoff = 5
        }
    }
}
