@echo off
REM Stop all Arasan Gas middleware services

echo Stopping Arasan Gas middleware services...

REM Kill continuous invoice fetch
taskkill /FI "WINDOWTITLE eq Arasan Gas - Invoice Fetch*" /F > nul 2>&1

REM Kill continuous sync
taskkill /FI "WINDOWTITLE eq Arasan Gas - Sync to Catalytics*" /F > nul 2>&1

echo All services stopped.
pause
