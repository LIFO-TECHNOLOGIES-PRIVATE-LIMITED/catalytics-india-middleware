@echo off
REM Continuously sync data every 30 seconds
REM Run this script at startup or manually

cd /d "%~dp0.."

echo Starting continuous sync to Catalytics (every 30 seconds)...
echo Press Ctrl+C to stop
echo.

:loop
echo [%date% %time%] Syncing to Catalytics...
py sync_to_catalytics.py

REM Wait 30 seconds
timeout /t 30 /nobreak > nul

goto loop
