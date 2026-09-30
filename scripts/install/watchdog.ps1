$ErrorActionPreference = "SilentlyContinue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $scriptDir "..\..\agent.py")) {
    $installDir = (Resolve-Path "$scriptDir\..\..").Path
} else {
    $installDir = $scriptDir
}

$python = Join-Path $installDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    $python = "pythonw.exe"
}
$agent = Join-Path $installDir "agent.py"

function Start-Agent($script) {
    return Start-Process `
        -FilePath $python `
        -ArgumentList @($script) `
        -WorkingDirectory $installDir `
        -PassThru
}

$desktopProcess = Start-Agent $agent

while ($true) {
    if ($desktopProcess.HasExited) {
        $desktopProcess = Start-Agent $agent
    }
    Start-Sleep -Seconds 5
}
