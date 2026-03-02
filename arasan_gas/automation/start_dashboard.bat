@echo off
REM Start Arasan Gas Middleware Dashboard
REM Opens web dashboard at http://localhost:5000

cd /d "%~dp0.."

echo ============================================================
echo ARASAN GAS MIDDLEWARE - STARTING DASHBOARD
echo ============================================================
echo.
echo Dashboard URL: http://localhost:5000
echo.
echo Press Ctrl+C to stop the dashboard
echo.

py dashboard.py
