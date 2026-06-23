@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ============================================================
echo   BHOX Middleware - Build and Deploy
echo   Building release with GUID-based DC sync + admin user ID
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
if exist "bhox_dashboard.spec" del /q "bhox_dashboard.spec"
echo [OK] Cleaned build artifacts

echo.
echo [3/6] Building dashboard EXE (no-console)...
py -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --onefile ^
    --noconsole ^
    --uac-admin ^
    --name bhox_dashboard ^
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
    --hidden-import log_capture ^
    --hidden-import fetch_master_data ^
    dashboard.py

if errorlevel 1 (
    echo [ERROR] EXE build failed
    exit /b 1
)
echo [OK] EXE built successfully

echo.
echo [4/6] Preparing release directory...
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd_HHmm')"') do set "STAMP=%%i"
set "RELEASE_DIR=%cd%\release\bhox_client_release_%STAMP%"
set "ZIP_PATH=%RELEASE_DIR%.zip"

if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%\logs"
echo [OK] Release directory created: %RELEASE_DIR%

echo.
echo [5/6] Copying client files...
copy /y "dist\bhox_dashboard.exe" "%RELEASE_DIR%\" >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy EXE
    exit /b 1
)

copy /y ".env" "%RELEASE_DIR%\.env" >nul
copy /y ".env.example" "%RELEASE_DIR%\.env.example" >nul
copy /y "version.json" "%RELEASE_DIR%\" >nul
copy /y "README_CLIENT_SETUP.txt" "%RELEASE_DIR%\" >nul
copy /y "RELEASE_NOTES.md" "%RELEASE_DIR%\" >nul
copy /y "Start_Dashboard.bat" "%RELEASE_DIR%\" >nul
copy /y "Stop_Dashboard.bat" "%RELEASE_DIR%\" >nul
copy /y "Install_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\" >nul

echo [OK] Files copied:
echo   - bhox_dashboard.exe
echo   - .env (current config)
echo   - .env.example
echo   - README_CLIENT_SETUP.txt
echo   - RELEASE_NOTES.md
echo   - Start_Dashboard.bat
echo   - Stop_Dashboard.bat
echo   - Install_AutoStart.bat
echo   - Remove_AutoStart.bat
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
echo What's New in This Release:
echo   - created_by / modified_by set from DEFAULT_ADMIN_USER_ID (env)
echo   - created_on set to actual invoice date (not server timestamp)
echo   - PO number / PO date: empty when not provided (no dc_no fallback)
echo   - Delivery/Customer Pickup filter on Other Reference field
echo   - GUID-based create/update for customers, products, and DCs
echo   - Batch invoice sync via /import/tally-dc-guid-payload/
echo   - Auto-fetch missing customers/products from Tally during invoice fetch
echo   - Smart DB path: auto-derives from ENTITY_NAME if dir/empty
echo.
echo Deployment Steps:
echo   1. Unzip on client system
echo   2. Review .env (set DEFAULT_ADMIN_USER_ID, ENTITY_ID, etc.)
echo   3. Double-click bhox_dashboard.exe
echo   4. Dashboard auto-creates logs and database
echo   5. Access dashboard at http://localhost:8787
echo.
echo Key .env Settings:
echo   DEFAULT_ADMIN_USER_ID=55   (sets created_by/modified_by in backend)
echo   ENTITY_ID=24               (Catalytics entity)
echo   INVOICE_FETCH_START_DATE   (YYYYMMDD, fetch from this date)
echo.
echo ============================================================

endlocal
pause
