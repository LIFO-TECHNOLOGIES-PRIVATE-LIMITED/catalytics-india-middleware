@echo off
setlocal

set "APP_NAME=COMiddlewareDashboard"
set "RUN_KEY=HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
set "EXE_PATH=%~dp0co_middleware_dashboard.exe"
set "ENV_PATH=%~dp0.env"

if not exist "%EXE_PATH%" (
    echo [ERROR] co_middleware_dashboard.exe not found in this folder.
    echo Place this script next to co_middleware_dashboard.exe and run again.
    exit /b 1
)

reg add "%RUN_KEY%" /v "%APP_NAME%" /t REG_SZ /d "\"%EXE_PATH%\"" /f >nul
if errorlevel 1 (
    echo [ERROR] Failed to set Windows startup entry.
    exit /b 1
)

reg add "HKCU\Environment" /v "TALLY_ENV_PATH" /t REG_SZ /d "%ENV_PATH%" /f >nul
if errorlevel 1 (
    echo [ERROR] Failed to set TALLY_ENV_PATH.
    exit /b 1
)

echo [OK] Auto-start enabled for current Windows user.
echo Middleware dashboard will start automatically on next login.
echo TALLY_ENV_PATH: %ENV_PATH%

endlocal
