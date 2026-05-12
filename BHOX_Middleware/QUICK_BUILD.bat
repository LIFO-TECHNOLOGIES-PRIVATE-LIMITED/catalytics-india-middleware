@echo off
REM Quick build script for v1.2.0
REM Just double-click this file to build the release

echo.
echo ========================================
echo   Quick Build - Version 1.2.0
echo ========================================
echo.

cd /d "%~dp0"

REM Check if BUILD_V1.2.0.bat exists
if not exist "BUILD_V1.2.0.bat" (
    echo [ERROR] BUILD_V1.2.0.bat not found!
    pause
    exit /b 1
)

REM Run the build
call BUILD_V1.2.0.bat

echo.
echo ========================================
echo   Build Complete!
echo ========================================
echo.
echo Next steps:
echo   1. Check release folder for ZIP file
echo   2. Read RELEASE_V1.2.0_SUMMARY.md
echo   3. Follow DEPLOY_V1.2.0.md for deployment
echo.

pause
