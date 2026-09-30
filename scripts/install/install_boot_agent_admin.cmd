@echo off
setlocal

fltmc.exe >nul 2>&1
if errorlevel 1 (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
        "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_boot_agent.ps1"
set "install_result=%errorlevel%"

if not "%install_result%"=="0" (
    echo.
    echo Installation failed with exit code %install_result%.
    pause
)

exit /b %install_result%
