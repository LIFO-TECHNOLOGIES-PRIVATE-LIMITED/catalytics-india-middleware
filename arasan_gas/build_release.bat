@echo off
setlocal

cd /d "%~dp0"

echo [1/5] Building dashboard EXE (no-console)...
call build_exe.bat
if errorlevel 1 (
    echo [ERROR] build_exe.bat failed.
    exit /b 1
)

echo [2/5] Preparing release directory...
for /f %%i in ('powershell -NoProfile -Command "(Get-Date).ToString('yyyyMMdd_HHmm')"') do set "STAMP=%%i"
set "RELEASE_DIR=%cd%\release\arasan_gas_client_release_%STAMP%"
set "ZIP_PATH=%RELEASE_DIR%.zip"

if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%"
mkdir "%RELEASE_DIR%\logs"

echo [3/5] Copying client files...
copy /y "dist\arasan_gas_dashboard.exe" "%RELEASE_DIR%\" >nul
copy /y ".env.example" "%RELEASE_DIR%\.env.example" >nul
copy /y ".env.example" "%RELEASE_DIR%\.env" >nul
copy /y "README_CLIENT_SETUP.txt" "%RELEASE_DIR%\" >nul
copy /y "Start_Dashboard.bat" "%RELEASE_DIR%\" >nul
copy /y "Install_AutoStart.bat" "%RELEASE_DIR%\" >nul
copy /y "Remove_AutoStart.bat" "%RELEASE_DIR%\" >nul

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
echo   2. Edit .env
echo   3. Double-click arasan_gas_dashboard.exe
echo   4. Startup auto-registers by default on first EXE launch (Install_AutoStart.bat is fallback)

endlocal
