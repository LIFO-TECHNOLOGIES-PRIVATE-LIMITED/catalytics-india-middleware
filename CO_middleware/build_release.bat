@echo off
setlocal

cd /d "%~dp0"
set "PYI_DIST_DIR=%cd%\dist_release"
set "PYI_BUILD_DIR=%cd%\build_release"

echo [1/5] Building dashboard EXE (no-console)...
call build_exe.bat
if errorlevel 1 (
    echo [ERROR] build_exe.bat failed.
    exit /b 1
)

echo [2/5] Preparing release directory...
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd_HHmm')"') do set "STAMP=%%i"
set "RELEASE_DIR=%cd%\release\co_middleware_client_release_%STAMP%"
set "ZIP_PATH=%RELEASE_DIR%.zip"

if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%\logs"

echo [3/5] Copying client files...
copy /y "%PYI_DIST_DIR%\co_middleware_dashboard.exe" "%RELEASE_DIR%\" >nul
if errorlevel 1 (
    echo [ERROR] Failed to copy EXE into release directory.
    exit /b 1
)
copy /y ".env.example" "%RELEASE_DIR%\.env.example" >nul
if exist ".env" (
    copy /y ".env" "%RELEASE_DIR%\.env" >nul
    echo [INFO] Packaged current .env into release.
) else (
    copy /y ".env.example" "%RELEASE_DIR%\.env" >nul
    echo [INFO] .env not found, packaged .env.example as release .env.
)
copy /y "README_CLIENT_SETUP.txt" "%RELEASE_DIR%\" >nul
if exist "RELEASE_NOTES.md" copy /y "RELEASE_NOTES.md" "%RELEASE_DIR%\" >nul
copy /y "Start_Dashboard.bat" "%RELEASE_DIR%\" >nul
copy /y "Install_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "allow_firewall.bat" "%RELEASE_DIR%\" >nul
copy /y "check_network_access.py" "%RELEASE_DIR%\" >nul

echo [4/5] Creating zip archive...
if exist "%ZIP_PATH%" del /q "%ZIP_PATH%"
powershell -NoProfile -Command "Compress-Archive -Path '%RELEASE_DIR%\*' -DestinationPath '%ZIP_PATH%' -Force"
if errorlevel 1 (
    echo [ERROR] Failed to create zip archive.
    exit /b 1
)

echo [5/5] Release build complete.
echo Folder: "%RELEASE_DIR%"
echo Zip:    "%ZIP_PATH%"

echo.
echo Client deployment steps:
echo   1. Unzip on client system
echo   2. Edit .env with Tally and Catalytics settings
echo   3. Double-click Start_Dashboard.bat or co_middleware_dashboard.exe
echo   4. Dashboard opens in browser at http://localhost:8787
echo   5. For network access, run allow_firewall.bat as Administrator
echo   6. Use Install_AutoStart.bat if Windows auto-start is required

endlocal