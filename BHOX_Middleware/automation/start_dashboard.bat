@echo off
REM Start BHOX Middleware Dashboard
REM Opens web dashboard at http://localhost:8787

cd /d "%~dp0.."

echo ============================================================
echo BHOX MIDDLEWARE - STARTING DASHBOARD
echo ============================================================
echo.
echo Dashboard URL: http://localhost:8787
echo.
echo Press Ctrl+C to stop the dashboard
echo.

py dashboard.py
