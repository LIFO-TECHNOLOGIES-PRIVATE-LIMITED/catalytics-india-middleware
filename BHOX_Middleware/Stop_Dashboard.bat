@echo off
echo ============================================================
echo   Stopping BHOX Middleware Dashboard
echo ============================================================
echo.

echo Searching for running dashboard process...
tasklist /FI "IMAGENAME eq bol_dashboard.exe" 2>NUL | find /I /N "bol_dashboard.exe">NUL
if "%ERRORLEVEL%"=="0" (
    echo Found running dashboard process. Stopping...
    taskkill /F /IM bol_dashboard.exe
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
