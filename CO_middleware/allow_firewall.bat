@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "PORT=8787"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if /I "%%~A"=="WEB_UI_PORT" if not "%%~B"=="" set "PORT=%%~B"
    )
)

set "EXE_PATH=%~dp0co_middleware_dashboard.exe"
set "RULE_PREFIX=CO Middleware Dashboard"

echo ============================================================
echo CO Middleware - Allow Firewall Access
echo ============================================================
echo.
echo This will add Windows Firewall rules for:
echo   - TCP port !PORT!
echo   - !EXE_PATH!
echo so other computers and services can access the dashboard.
echo.
echo You need to run this as Administrator!
echo.
pause

netsh advfirewall firewall delete rule name="!RULE_PREFIX! Port !PORT!" >nul 2>&1
netsh advfirewall firewall delete rule name="!RULE_PREFIX! Inbound EXE" >nul 2>&1
netsh advfirewall firewall delete rule name="!RULE_PREFIX! Outbound EXE" >nul 2>&1

netsh advfirewall firewall add rule name="!RULE_PREFIX! Port !PORT!" dir=in action=allow protocol=TCP localport=!PORT! enable=yes >nul
if errorlevel 1 goto :failed

if exist "!EXE_PATH!" (
    netsh advfirewall firewall add rule name="!RULE_PREFIX! Inbound EXE" dir=in action=allow program="!EXE_PATH!" enable=yes >nul
    if errorlevel 1 goto :failed

    netsh advfirewall firewall add rule name="!RULE_PREFIX! Outbound EXE" dir=out action=allow program="!EXE_PATH!" enable=yes >nul
    if errorlevel 1 goto :failed
)

echo.
echo ============================================================
echo SUCCESS! Firewall rules added.
echo ============================================================
echo.
echo The dashboard is now accessible from other computers at:
echo   http://YOUR_IP:!PORT!
echo.
echo To find your IP address, run: ipconfig
echo Look for "IPv4 Address" under your network adapter
echo.
goto :done

:failed
echo.
echo ============================================================
echo FAILED! Could not add firewall rules.
echo ============================================================
echo.
echo Please run this script as Administrator:
echo   1. Right-click on allow_firewall.bat
echo   2. Select "Run as administrator"
echo.

:done
pause
endlocal