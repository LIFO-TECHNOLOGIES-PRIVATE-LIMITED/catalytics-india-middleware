@echo off
echo ============================================================
echo   Stopping Arasan Gas Middleware Dashboard
echo ============================================================
echo.

echo Searching for running dashboard process...
tasklist /FI "IMAGENAME eq arasan_gas_dashboard.exe" 2>NUL | find /I /N "arasan_gas_dashboard.exe">NUL
if "%ERRORLEVEL%"=="0" (
    echo Found running dashboard process. Stopping...
    taskkill /F /IM arasan_gas_dashboard.exe
    if errorlevel 1 (
        echo [ERROR] Failed to stop dashboard
        echo Try running this script as Administrator
    ) else (
        echo [SUCCESS] Dashboard stopped successfully
    )
) else (
    echo [INFO] Dashboard is not running
)

echo.
echo ============================================================
pause
