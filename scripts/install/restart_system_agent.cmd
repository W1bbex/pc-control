@echo off
echo Перезапуск PC Control System Agent...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0restart_system_agent.ps1\"'"
echo Запрос UAC отправлен. Нажмите Да в окне подтверждения Windows.
