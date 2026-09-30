@echo off
chcp 65001 >nul
title PCControl - Code Backup Tool
setlocal

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=python"

echo ========================================================
echo       PCControl Automated Code Backup Runner
echo ========================================================
echo.
"%PYTHON_EXE%" "%SCRIPT_DIR%auto_backup.py" %*
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo Нажмите любую клавишу для выхода...
pause >nul
exit /b %ERRORLEVEL%
