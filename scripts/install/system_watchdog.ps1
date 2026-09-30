$ErrorActionPreference = "SilentlyContinue"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path (Join-Path $scriptDir "..\..\boot_agent.py")) {
    $installDir = (Resolve-Path "$scriptDir\..\..").Path
} else {
    $installDir = $scriptDir
}

$python = Join-Path $installDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    $python = "pythonw.exe"
}
$systemAgent = Join-Path $installDir "boot_agent.py"

while ($true) {
    $p = Start-Process -FilePath $python -ArgumentList @($systemAgent) -WorkingDirectory $installDir -PassThru
    Wait-Process -Id $p.Id
    Start-Sleep -Seconds 5
}
