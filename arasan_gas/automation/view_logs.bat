@echo off
REM View Arasan Gas middleware logs

cd /d "%~dp0.."

:menu
cls
echo ============================================================
echo ARASAN GAS MIDDLEWARE - LOG VIEWER
echo ============================================================
echo.
echo Select log to view:
echo.
echo 1. Main Application Log (arasan_gas.log)
echo 2. Master Data Fetch Log (fetch_master_data.log)
echo 3. Invoice Fetch Log (fetch_invoices.log)
echo 4. Sync Log (sync_to_catalytics.log)
echo 5. Search logs for errors
echo 6. View last 50 lines of all logs
echo 7. Exit
echo.
set /p choice="Enter choice (1-7): "

if "%choice%"=="1" (
    cls
    echo === Main Application Log ===
    echo.
    type logs\arasan_gas.log 2>nul | more
    pause
    goto menu
)
if "%choice%"=="2" (
    cls
    echo === Master Data Fetch Log ===
    echo.
    type logs\fetch_master_data.log 2>nul | more
    pause
    goto menu
)
if "%choice%"=="3" (
    cls
    echo === Invoice Fetch Log ===
    echo.
    type logs\fetch_invoices.log 2>nul | more
    pause
    goto menu
)
if "%choice%"=="4" (
    cls
    echo === Sync Log ===
    echo.
    type logs\sync_to_catalytics.log 2>nul | more
    pause
    goto menu
)
if "%choice%"=="5" (
    cls
    echo === Searching for ERRORS in all logs ===
    echo.
    findstr /I /C:"ERROR" logs\*.log 2>nul
    echo.
    pause
    goto menu
)
if "%choice%"=="6" (
    cls
    echo === Last 50 lines of all logs ===
    echo.
    echo [arasan_gas.log]
    powershell -command "Get-Content logs\arasan_gas.log -Tail 10 -ErrorAction SilentlyContinue"
    echo.
    echo [fetch_master_data.log]
    powershell -command "Get-Content logs\fetch_master_data.log -Tail 10 -ErrorAction SilentlyContinue"
    echo.
    echo [fetch_invoices.log]
    powershell -command "Get-Content logs\fetch_invoices.log -Tail 10 -ErrorAction SilentlyContinue"
    echo.
    echo [sync_to_catalytics.log]
    powershell -command "Get-Content logs\sync_to_catalytics.log -Tail 10 -ErrorAction SilentlyContinue"
    echo.
    pause
    goto menu
)
if "%choice%"=="7" exit

goto menu
