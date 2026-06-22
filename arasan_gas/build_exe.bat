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
if exist "arasan_gas_dashboard.spec" del /q "arasan_gas_dashboard.spec"

echo [3/4] Building middleware dashboard EXE...
py -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --noconsole ^
    --name arasan_gas_dashboard ^
    --add-data "templates;templates" ^
    --add-data "version.json;." ^
    --collect-all psycopg2 ^
    --hidden-import fetch_customers ^
    --hidden-import fetch_products ^
    --hidden-import fetch_invoices ^
    --hidden-import sync_to_catalytics ^
    --hidden-import verify_sync ^
    --hidden-import automation_manager ^
    --hidden-import data_matcher ^
    dashboard.py

if errorlevel 1 (
    echo [ERROR] EXE build failed.
    exit /b 1
)

echo [4/4] Build complete.
echo EXE generated at: "%cd%\dist\arasan_gas_dashboard.exe"
echo.
echo Place these next to the EXE on client machine:
echo   - .env
echo   - arasan_gas.sqlite (optional, auto-created if missing)
echo   - logs\ folder (optional, auto-created if missing)

endlocal
