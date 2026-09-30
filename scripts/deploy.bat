@echo off
chcp 65001 >nul
title PCControl - VDS Deploy Tool
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "PYTHON_EXE=python"

echo ========================================================
echo       PCControl VDS Deployment & Sync Runner
echo ========================================================
echo.

if "%~1"=="" (
    echo Выберите действие:
    echo   [1] Deploy  - Залить код на VDS, проверить и перезапустить сервис
    echo   [2] Diff    - Показать различия между ПК и VDS
    echo   [3] Pull    - Скачать актуальный код с VDS на ПК
    echo   [4] Status  - Проверить статус службы и последние логи на VDS
    echo   [5] Rollback- Откатить VDS на предыдущий рабочий релиз
    echo.
    set /p "CHOICE=Введите номер [1-5] (по умолчанию 1): "
    if "!CHOICE!"=="" set "CHOICE=1"

    if "!CHOICE!"=="1" set "ACTION=deploy"
    if "!CHOICE!"=="2" set "ACTION=diff"
    if "!CHOICE!"=="3" set "ACTION=pull"
    if "!CHOICE!"=="4" set "ACTION=status"
    if "!CHOICE!"=="5" set "ACTION=rollback"
) else (
    set "ACTION=%~1"
)

echo.
echo [*] Запуск: %PYTHON_EXE% "%SCRIPT_DIR%deploy_vds.py" !ACTION!
"%PYTHON_EXE%" "%SCRIPT_DIR%deploy_vds.py" !ACTION!
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if !EXIT_CODE! equ 0 (
    echo [✓] Операция успешно завершена!
) else (
    echo [!] Произошла ошибка (код !EXIT_CODE!).
)

echo.
echo Нажмите любую клавишу для выхода...
pause >nul
exit /b !EXIT_CODE!
