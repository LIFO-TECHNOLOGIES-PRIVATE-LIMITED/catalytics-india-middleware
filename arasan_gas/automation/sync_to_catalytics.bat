@echo off
REM Sync data to Catalytics backend
REM Run this every 30 seconds

cd /d "%~dp0.."

py sync_to_catalytics.py >> logs\sync_to_catalytics.log 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] ERROR: Sync to Catalytics failed >> logs\sync_to_catalytics.log
)
