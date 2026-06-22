@echo off
setlocal

cd /d "%~dp0"

if not defined PYI_DIST_DIR set "PYI_DIST_DIR=%cd%\dist"
if not defined PYI_BUILD_DIR set "PYI_BUILD_DIR=%cd%\build"

echo [1/4] Checking Python and PyInstaller...
py --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH.
    exit /b 1
)

py -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller is not installed. Run: py -m pip install pyinstaller
    exit /b 1
)

echo [2/4] Cleaning previous build artifacts...
if exist "%PYI_BUILD_DIR%" rmdir /s /q "%PYI_BUILD_DIR%"
if exist "%PYI_DIST_DIR%" rmdir /s /q "%PYI_DIST_DIR%"
if exist "co_middleware_dashboard.spec" del /q "co_middleware_dashboard.spec"

echo [3/4] Building CO Middleware dashboard EXE...
py -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --noconsole ^
    --name co_middleware_dashboard ^
    --distpath "%PYI_DIST_DIR%" ^
    --workpath "%PYI_BUILD_DIR%" ^
    --add-data "templates;templates" ^
    --add-data ".env.example;." ^
    --add-data "version.json;." ^
    --collect-all psycopg2 ^
    --hidden-import config ^
    --hidden-import db ^
    --hidden-import tally_api ^
    --hidden-import tally_client ^
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
echo EXE generated at: "%PYI_DIST_DIR%\co_middleware_dashboard.exe"
echo.
echo First launch behavior:
echo   - .env is auto-created from bundled defaults if missing
echo   - SQLite DB file(s) and tables are auto-created if missing
echo   - logs\ folder and standard log files are auto-created if missing
echo You can still place a custom .env next to the EXE before launch.

endlocal
