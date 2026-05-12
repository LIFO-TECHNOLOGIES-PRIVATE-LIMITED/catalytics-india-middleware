@echo off
REM Stop all BHOX middleware services

echo Stopping BHOX middleware services...

REM Kill continuous invoice fetch
taskkill /FI "WINDOWTITLE eq BHOX - Invoice Fetch*" /F > nul 2>&1

REM Kill continuous sync
taskkill /FI "WINDOWTITLE eq BHOX - Sync to Catalytics*" /F > nul 2>&1

echo All services stopped.
pause
