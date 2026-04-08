@echo off
REM Start all BOL middleware services
REM Run this at system startup

cd /d "%~dp0.."

echo ============================================================
echo Starting BOL Middleware Services
echo ============================================================
echo.

REM Check if Tally is accessible
echo Checking Tally connection...
curl -s --connect-timeout 5 http://localhost:9000/ > nul 2>&1
if %errorlevel% neq 0 (
    echo WARNING: Tally server not accessible at localhost:9000
    echo Please ensure Tally is running before starting services.
    pause
    exit /b 1
)
echo Tally server: OK

REM Check if Catalytics backend is accessible
echo Checking Catalytics backend connection...
curl -s --connect-timeout 5 http://localhost:8000/ > nul 2>&1
if %errorlevel% neq 0 (
    echo WARNING: Catalytics backend not accessible at localhost:8000
    echo Please ensure Django backend is running before starting services.
    pause
    exit /b 1
)
echo Catalytics backend: OK
echo.

REM Create logs directory if not exists
if not exist "logs" mkdir logs

REM Start continuous invoice fetch in new window
echo Starting continuous invoice fetch service...
start "BOL - Invoice Fetch" /MIN cmd /c "%~dp0continuous_fetch_invoices.bat"

REM Wait 2 seconds
timeout /t 2 /nobreak > nul

REM Start continuous sync in new window
echo Starting continuous sync service...
start "BOL - Sync to Catalytics" /MIN cmd /c "%~dp0continuous_sync.bat"

echo.
echo ============================================================
echo All services started successfully!
echo ============================================================
echo.
echo Services running:
echo   1. Invoice Fetch (every 30 seconds)
echo   2. Sync to Catalytics (every 30 seconds)
echo.
echo To stop services: Close the minimized windows or use stop_all_services.bat
echo.
pause
