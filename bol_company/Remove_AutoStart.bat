@echo off
setlocal

set "APP_NAME=BOLMiddlewareDashboard"
set "RUN_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"

rem Remove the Scheduled Task created by the dashboard on startup.
schtasks /Delete /TN "%APP_NAME%" /F >nul 2>&1
if errorlevel 1 (
    echo [INFO] Scheduled task was not present.
) else (
    echo [OK] Scheduled task removed.
)

rem Also clean up any stale HKCU Run entry from older builds.
reg delete "%RUN_KEY%" /v "%APP_NAME%" /f >nul 2>&1
if errorlevel 1 (
    echo [INFO] Run-key auto-start entry was not present.
) else (
    echo [OK] Run-key auto-start entry removed.
)

endlocal
