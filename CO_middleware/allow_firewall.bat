@echo off
echo ============================================================
echo CO Middleware - Allow Firewall Access
echo ============================================================
echo.
echo This will add a Windows Firewall rule to allow port 8787
echo so other computers can access the dashboard.
echo.
echo You need to run this as Administrator!
echo.
pause

netsh advfirewall firewall add rule name="CO Middleware Dashboard" dir=in action=allow protocol=TCP localport=8787

if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo SUCCESS! Firewall rule added.
    echo ============================================================
    echo.
    echo The dashboard is now accessible from other computers at:
    echo   http://YOUR_IP:8787
    echo.
    echo To find your IP address, run: ipconfig
    echo Look for "IPv4 Address" under your network adapter
    echo.
) else (
    echo.
    echo ============================================================
    echo FAILED! Could not add firewall rule.
    echo ============================================================
    echo.
    echo Please run this script as Administrator:
    echo   1. Right-click on allow_firewall.bat
    echo   2. Select "Run as administrator"
    echo.
)

pause
