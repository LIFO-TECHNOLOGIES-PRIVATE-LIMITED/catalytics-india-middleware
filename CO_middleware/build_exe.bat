@echo off
setlocal

cd /d "%~dp0"

echo [1/4] Checking PyInstaller...
py -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller is not installed. Run: py -m pip install pyinstaller
    exit /b 1
)

echo [2/4] Cleaning previous build artifacts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "co_middleware_dashboard.spec" del /q "co_middleware_dashboard.spec"

echo [3/4] Building CO Middleware dashboard EXE...
py -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --noconsole ^
    --name co_middleware_dashboard ^
    --add-data "templates;templates" ^
    --hidden-import config ^
    --hidden-import db ^
    --hidden-import tally_api ^
    --hidden-import tally_client ^
    --hidden-import fetch_tally ^
    --hidden-import fetch_invoices ^
    --hidden-import fetch_customers ^
    --hidden-import fetch_products ^
    --hidden-import sync_catalytics ^
    --hidden-import sync_customers ^
    --hidden-import sync_products ^
    --hidden-import automation_manager ^
    --hidden-import log_capture ^
    --hidden-import logging_utils ^
    dashboard.py

if errorlevel 1 (
    echo [ERROR] EXE build failed.
    exit /b 1
)

echo [4/4] Build complete.
echo EXE generated at: "%cd%\dist\co_middleware_dashboard.exe"
echo.
echo Place these next to the EXE on client machine:
echo   - .env
echo   - tally_dc.sqlite (optional, auto-created if missing)
echo   - logs\ folder (optional, auto-created if missing)

endlocal
