@echo off
echo Starting CO Middleware Dashboard...
echo.
echo Dashboard will open in your browser at http://localhost:8787
echo.
echo Press Ctrl+C to stop the dashboard
echo.

cd /d "%~dp0"
set TALLY_ENV_PATH=%~dp0.env
echo Using .env file: %TALLY_ENV_PATH%
echo.
co_middleware_dashboard.exe

pause
