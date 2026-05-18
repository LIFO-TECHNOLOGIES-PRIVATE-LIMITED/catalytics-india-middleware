@echo off
echo ============================================================
echo   Building BHOX Middleware v1.2.0
echo   Customer Table Fix + Diagnostics Release
echo ============================================================
echo.

cd /d "%~dp0"

echo [INFO] Copying diagnostic tools to release...
echo.

REM Run the main build script
call BUILD_AND_DEPLOY.bat

if errorlevel 1 (
    echo [ERROR] Build failed
    exit /b 1
)

echo.
echo ============================================================
echo   POST-BUILD: Adding Diagnostic Tools
echo ============================================================
echo.

REM Get the latest release directory
for /f "delims=" %%i in ('dir /b /ad /o-d release\bol_client_release_*') do (
    set "LATEST_RELEASE=%%i"
    goto :found
)
:found

set "RELEASE_DIR=%cd%\release\%LATEST_RELEASE%"

echo Release directory: %RELEASE_DIR%
echo.

echo Copying diagnostic tools...
copy /y "check_pending_invoices.py" "%RELEASE_DIR%\" >nul
copy /y "reset_failed_invoices.py" "%RELEASE_DIR%\" >nul
copy /y "test_single_invoice_sync.py" "%RELEASE_DIR%\" >nul
copy /y "DEPLOY_V1.2.0.md" "%RELEASE_DIR%\DEPLOYMENT_GUIDE.md" >nul

echo [OK] Diagnostic tools copied:
echo   - check_pending_invoices.py
echo   - reset_failed_invoices.py
echo   - test_single_invoice_sync.py
echo   - DEPLOYMENT_GUIDE.md
echo.

echo ============================================================
echo   BUILD COMPLETE - Version 1.2.0
echo ============================================================
echo.
echo Release Location: %RELEASE_DIR%
echo.
echo What's New in v1.2.0:
echo   - Fixed Customer table case-sensitivity issue
echo   - Added diagnostic tools for troubleshooting
echo   - Improved error handling and logging
echo   - Database table name auto-fix script
echo.
echo Deployment Steps:
echo   1. Deploy backend first (see DEPLOYMENT_GUIDE.md)
echo   2. Restart Django server completely
echo   3. Run fix_customer_table_name.py if needed
echo   4. Deploy middleware release to client
echo   5. Test invoice sync
echo.
echo ============================================================

pause
