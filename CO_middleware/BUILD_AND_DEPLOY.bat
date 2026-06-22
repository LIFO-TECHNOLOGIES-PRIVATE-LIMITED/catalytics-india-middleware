@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ============================================================
echo   CO Middleware - Build and Deploy
echo ============================================================
echo.

echo [1/6] Checking Python and PyInstaller...
py --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH
    exit /b 1
)

py -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PyInstaller is not installed. Run: py -m pip install pyinstaller
    exit /b 1
)
echo [OK] Python and PyInstaller found

echo.
echo [2/6] Cleaning previous build artifacts...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "co_middleware_dashboard.spec" del /q "co_middleware_dashboard.spec"
echo [OK] Cleaned build artifacts

echo.
echo [3/6] Building dashboard EXE (no-console)...
py -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --noconsole ^
    --name co_middleware_dashboard ^
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
    --hidden-import mapping_lookup ^
    dashboard.py

if errorlevel 1 (
    echo [ERROR] EXE build failed
    exit /b 1
)
echo [OK] EXE built successfully

echo.
echo [4/6] Preparing release directory...
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd_HHmm')"') do set "STAMP=%%i"
set "RELEASE_DIR=%cd%\release\co_middleware_client_release_%STAMP%"
set "ZIP_PATH=%RELEASE_DIR%.zip"

if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%\logs"
echo [OK] Release directory created: %RELEASE_DIR%

echo.
echo [5/6] Copying client files...
copy /y "dist\co_middleware_dashboard.exe" "%RELEASE_DIR%\" >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy EXE
    exit /b 1
)

if exist ".env" (
    copy /y ".env" "%RELEASE_DIR%\.env" >nul
    echo [INFO] Packaged current .env into release.
) else (
    copy /y ".env.example" "%RELEASE_DIR%\.env" >nul
    echo [INFO] .env not found, packaged .env.example as release .env.
)
copy /y ".env.example" "%RELEASE_DIR%\.env.example" >nul
copy /y "version.json" "%RELEASE_DIR%\" >nul
copy /y "README_CLIENT_SETUP.txt" "%RELEASE_DIR%\" >nul
if exist "RELEASE_NOTES.md" copy /y "RELEASE_NOTES.md" "%RELEASE_DIR%\" >nul
copy /y "Start_Dashboard.bat" "%RELEASE_DIR%\" >nul
copy /y "Install_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\" >nul
if exist "allow_firewall.bat" copy /y "allow_firewall.bat" "%RELEASE_DIR%\" >nul

echo [OK] Files copied:
echo   - co_middleware_dashboard.exe
echo   - .env (current config)
echo   - .env.example
echo   - README_CLIENT_SETUP.txt
echo   - RELEASE_NOTES.md
echo   - Start_Dashboard.bat
echo   - Install_AutoStart.bat
echo   - Remove_AutoStart.bat
echo   - allow_firewall.bat
echo   - logs\ (empty folder)

echo.
echo [6/6] Creating zip archive...
if exist "%ZIP_PATH%" del /q "%ZIP_PATH%"
powershell -NoProfile -Command "Compress-Archive -Path '%RELEASE_DIR%\*' -DestinationPath '%ZIP_PATH%' -Force"
if errorlevel 1 (
    echo [ERROR] Failed to create zip archive
    exit /b 1
)
echo [OK] Zip archive created

echo.
echo ============================================================
echo   BUILD COMPLETE!
echo ============================================================
echo.
echo Release Location:
echo   Folder: %RELEASE_DIR%
echo   Zip:    %ZIP_PATH%
echo.
echo Deployment Steps:
echo   1. Unzip on client system
echo   2. Edit .env (set ENTITY_ID, TALLY_URL, CATALYTICS_API_KEY, etc.)
echo   3. Double-click co_middleware_dashboard.exe or Start_Dashboard.bat
echo   4. Dashboard opens in browser at http://localhost:8787
echo   5. For network access, run allow_firewall.bat as Administrator
echo   6. Use Install_AutoStart.bat if Windows auto-start is required
echo.
echo Key .env Settings:
echo   ENTITY_ID=29                  (Catalytics entity)
echo   TALLY_URL=http://localhost:9000/
echo   CATALYTICS_API_BASE_URL=...   (Catalytics API endpoint)
echo   CATALYTICS_API_KEY=...        (API auth token)
echo   FETCH_SYNC_INTERVAL_SECONDS=10 (DC fetch+sync every 10s)
echo   MASTER_SYNC_TIME=10:00        (daily master sync at 10 AM)
echo.
echo ============================================================

endlocal
pause
