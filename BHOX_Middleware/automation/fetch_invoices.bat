@echo off
REM Fetch invoices from Tally
REM Run this every 30 seconds

cd /d "%~dp0.."

REM Fetch today's invoices
py fetch_invoices.py >> logs\fetch_invoices.log 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] ERROR: Invoice fetch failed >> logs\fetch_invoices.log
)
