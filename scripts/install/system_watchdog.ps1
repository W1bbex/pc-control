$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $scriptDir "..\..\boot_agent.py")) {
    $installDir = (Resolve-Path "$scriptDir\..\..").Path
} else {
    $installDir = $scriptDir
}

$logDir = Join-Path $installDir "logs"
if (-not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}
$logFile = Join-Path $logDir "watchdog_system.log"

$python = Join-Path $installDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    $python = "pythonw.exe"
}
$systemAgent = Join-Path $installDir "boot_agent.py"

$backoff = 5
while ($true) {
    try {
        $startedAt = Get-Date
        $p = Start-Process -FilePath $python -ArgumentList @('"' + $systemAgent + '"') -WorkingDirectory $installDir -PassThru
        if (-not $p) {
            throw "Start-Process returned null for $systemAgent"
        }
        Wait-Process -Id $p.Id
        $uptime = ((Get-Date) - $startedAt).TotalSeconds
        if ($uptime -lt 15) {
            Start-Sleep -Seconds $backoff
            $backoff = [Math]::Min(60, $backoff * 2)
        } else {
            $backoff = 5
            Start-Sleep -Seconds 5
        }
    } catch {
        Add-Content -Path $logFile -Value "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERROR: $_" -Encoding utf8
        Start-Sleep -Seconds $backoff
        $backoff = [Math]::Min(60, $backoff * 2)
    }
}
