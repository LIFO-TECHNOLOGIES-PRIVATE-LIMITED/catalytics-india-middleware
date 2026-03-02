@echo off
REM Install Windows Task Scheduler tasks for Arasan Gas middleware
REM Run this as Administrator

echo ============================================================
echo Installing Windows Task Scheduler Tasks
echo ============================================================
echo.
echo This will create the following scheduled tasks:
echo   1. Arasan_Gas_Fetch_Master_Data (every 10 minutes)
echo   2. Arasan_Gas_Fetch_Invoices (every 1 minute, runs twice)
echo   3. Arasan_Gas_Sync_To_Catalytics (every 1 minute, runs twice)
echo.
echo Press any key to continue or Ctrl+C to cancel...
pause > nul

REM Get the current directory
set MIDDLEWARE_PATH=%~dp0..
set AUTOMATION_PATH=%~dp0

echo.
echo Installing tasks...
echo.

REM Task 1: Fetch master data every 10 minutes
echo Creating task: Arasan_Gas_Fetch_Master_Data
schtasks /create /tn "Arasan_Gas_Fetch_Master_Data" /tr "\"%AUTOMATION_PATH%fetch_master_data.bat\"" /sc minute /mo 10 /f /rl highest
if %errorlevel% equ 0 (
    echo   SUCCESS
) else (
    echo   FAILED - Run this script as Administrator
)

REM Task 2: Fetch invoices every 30 seconds (using 2 tasks offset by 30 seconds)
echo Creating task: Arasan_Gas_Fetch_Invoices_1
schtasks /create /tn "Arasan_Gas_Fetch_Invoices_1" /tr "\"%AUTOMATION_PATH%fetch_invoices.bat\"" /sc minute /mo 1 /f /rl highest
if %errorlevel% equ 0 (
    echo   SUCCESS
) else (
    echo   FAILED - Run this script as Administrator
)

echo Creating task: Arasan_Gas_Fetch_Invoices_2 (30 sec delay)
schtasks /create /tn "Arasan_Gas_Fetch_Invoices_2" /tr "\"%AUTOMATION_PATH%fetch_invoices.bat\"" /sc minute /mo 1 /f /rl highest /delay 0000:30
if %errorlevel% equ 0 (
    echo   SUCCESS
) else (
    echo   FAILED - Run this script as Administrator
)

REM Task 3: Sync every 30 seconds (using 2 tasks offset by 30 seconds)
echo Creating task: Arasan_Gas_Sync_To_Catalytics_1
schtasks /create /tn "Arasan_Gas_Sync_To_Catalytics_1" /tr "\"%AUTOMATION_PATH%sync_to_catalytics.bat\"" /sc minute /mo 1 /f /rl highest
if %errorlevel% equ 0 (
    echo   SUCCESS
) else (
    echo   FAILED - Run this script as Administrator
)

echo Creating task: Arasan_Gas_Sync_To_Catalytics_2 (30 sec delay)
schtasks /create /tn "Arasan_Gas_Sync_To_Catalytics_2" /tr "\"%AUTOMATION_PATH%sync_to_catalytics.bat\"" /sc minute /mo 1 /f /rl highest /delay 0000:30
if %errorlevel% equ 0 (
    echo   SUCCESS
) else (
    echo   FAILED - Run this script as Administrator
)

echo.
echo ============================================================
echo Installation complete!
echo ============================================================
echo.
echo Scheduled tasks have been created. To manage them:
echo   1. Open Task Scheduler (taskschd.msc)
echo   2. Look for tasks starting with "Arasan_Gas_"
echo.
echo To uninstall, run: uninstall_task_scheduler.bat
echo.
pause
