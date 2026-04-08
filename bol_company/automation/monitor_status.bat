@echo off
REM Monitor BOL middleware status
REM Shows sync statistics and recent activity

cd /d "%~dp0.."

:loop
cls
echo ============================================================
echo BOL MIDDLEWARE - LIVE MONITOR
echo ============================================================
echo Current Time: %date% %time%
echo.

REM Check Tally connection
echo [CONNECTIONS]
curl -s --connect-timeout 2 http://localhost:9000/ > nul 2>&1
if %errorlevel% equ 0 (
    echo Tally Server:      [ONLINE]  localhost:9000
) else (
    echo Tally Server:      [OFFLINE] localhost:9000
)

curl -s --connect-timeout 2 http://localhost:8000/ > nul 2>&1
if %errorlevel% equ 0 (
    echo Catalytics Backend: [ONLINE]  localhost:8000
) else (
    echo Catalytics Backend: [OFFLINE] localhost:8000
)

echo.
echo [DATABASE STATISTICS]
py -c "from db import Database; from config import config; db = Database(config.SQLITE_DB_PATH); stats = db.get_statistics(); print(f'Customers:  {stats[\"total_customers\"]} total, {stats[\"synced_customers\"]} synced'); print(f'Products:   {stats[\"total_products\"]} total, {stats[\"synced_products\"]} synced'); print(f'Invoices:   {stats[\"total_invoices\"]} total, {stats[\"synced_invoices\"]} synced'); print(f'Duplicates: {stats[\"total_duplicates\"]} logged'); db.close()" 2>nul
if %errorlevel% neq 0 (
    echo Error reading database
)

echo.
echo [RECENT ACTIVITY - Last 10 lines]
echo.
type logs\bol.log 2>nul | find /V "" | more +0 > temp_log.txt
for /f "skip=0 tokens=*" %%a in (temp_log.txt) do (
    set /a count+=1
    if !count! gtr -10 echo %%a
)
del temp_log.txt 2>nul

echo.
echo ============================================================
echo Press Ctrl+C to exit or wait for auto-refresh (30 sec)
echo ============================================================

timeout /t 30 /nobreak > nul
goto loop
