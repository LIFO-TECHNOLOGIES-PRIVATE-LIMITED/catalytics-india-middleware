@echo off
REM Fetch master data (customers and products) from Tally
REM Run this every 10 minutes

cd /d "%~dp0.."

echo [%date% %time%] Starting master data fetch...

REM Fetch customers
py fetch_customers.py >> logs\fetch_master_data.log 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] ERROR: Customer fetch failed >> logs\fetch_master_data.log
)

REM Fetch products
py fetch_products.py >> logs\fetch_master_data.log 2>&1
if %errorlevel% neq 0 (
    echo [%date% %time%] ERROR: Product fetch failed >> logs\fetch_master_data.log
)

echo [%date% %time%] Master data fetch completed >> logs\fetch_master_data.log
