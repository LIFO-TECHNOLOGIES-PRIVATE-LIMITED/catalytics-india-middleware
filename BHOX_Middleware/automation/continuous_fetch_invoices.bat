@echo off
REM Continuously fetch invoices every 30 seconds
REM Run this script at startup or manually

cd /d "%~dp0.."

echo Starting continuous invoice fetch (every 30 seconds)...
echo Press Ctrl+C to stop
echo.

:loop
echo [%date% %time%] Fetching invoices...
py fetch_invoices.py

REM Wait 30 seconds
timeout /t 30 /nobreak > nul

goto loop
