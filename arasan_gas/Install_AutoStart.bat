@echo off
setlocal

set "APP_NAME=ArasanGasMiddlewareDashboard"
set "RUN_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
set "EXE_PATH=%~dp0arasan_gas_dashboard.exe"

if not exist "%EXE_PATH%" (
    echo [ERROR] arasan_gas_dashboard.exe not found in this folder.
    echo Place this script next to arasan_gas_dashboard.exe and run again.
    exit /b 1
)

reg add "%RUN_KEY%" /v "%APP_NAME%" /t REG_SZ /d "\"%EXE_PATH%\"" /f >nul
if errorlevel 1 (
    echo [ERROR] Failed to set Windows startup entry.
    exit /b 1
)

echo [OK] Auto-start enabled for current Windows user.
echo Middleware dashboard will start automatically on next login.

endlocal
