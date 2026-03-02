@echo off
setlocal

set "APP_NAME=ArasanGasMiddlewareDashboard"
set "RUN_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"

reg delete "%RUN_KEY%" /v "%APP_NAME%" /f >nul 2>&1
if errorlevel 1 (
    echo [INFO] Auto-start entry was not present.
) else (
    echo [OK] Auto-start entry removed.
)

endlocal
