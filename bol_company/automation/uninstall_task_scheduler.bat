@echo off
REM Uninstall Windows Task Scheduler tasks for BOL middleware
REM Run this as Administrator

echo ============================================================
echo Uninstalling Windows Task Scheduler Tasks
echo ============================================================
echo.
echo This will remove all BOL scheduled tasks.
echo.
echo Press any key to continue or Ctrl+C to cancel...
pause > nul

echo.
echo Removing tasks...
echo.

schtasks /delete /tn "BOL_Gas_Fetch_Master_Data" /f
schtasks /delete /tn "BOL_Gas_Fetch_Invoices_1" /f
schtasks /delete /tn "BOL_Gas_Fetch_Invoices_2" /f
schtasks /delete /tn "BOL_Gas_Sync_To_Catalytics_1" /f
schtasks /delete /tn "BOL_Gas_Sync_To_Catalytics_2" /f

echo.
echo All tasks removed.
echo.
pause
