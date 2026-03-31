@echo off
cd /d "%~dp0"

if not exist "%~dp0co_middleware_dashboard.exe" (
    echo [ERROR] co_middleware_dashboard.exe not found in this folder.
    echo Place this script next to co_middleware_dashboard.exe and run again.
    exit /b 1
)

set "TALLY_ENV_PATH=%~dp0.env"
start "" "%~dp0co_middleware_dashboard.exe"
