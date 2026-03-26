@echo off
REM Start BOL Middleware Dashboard
REM Opens web dashboard at http://localhost:8787

cd /d "%~dp0.."

echo ============================================================
echo BOL MIDDLEWARE - STARTING DASHBOARD
echo ============================================================
echo.
echo Dashboard URL: http://localhost:8787
echo.
echo Press Ctrl+C to stop the dashboard
echo.

py dashboard.py
