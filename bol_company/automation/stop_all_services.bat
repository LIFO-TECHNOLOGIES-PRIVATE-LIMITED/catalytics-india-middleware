@echo off
REM Stop all BOL middleware services

echo Stopping BOL middleware services...

REM Kill continuous invoice fetch
taskkill /FI "WINDOWTITLE eq BOL - Invoice Fetch*" /F > nul 2>&1

REM Kill continuous sync
taskkill /FI "WINDOWTITLE eq BOL - Sync to Catalytics*" /F > nul 2>&1

echo All services stopped.
pause
