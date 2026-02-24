@echo off
setlocal

set ROOT=%~dp0
set DIST=%ROOT%\dist
set RELEASE=%~dp0release
set PACKAGE=%RELEASE%\package

if not exist "%DIST%\tally_fetch.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_fetch_invoices.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_fetch_customers.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_sync_customers.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_fetch_products.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_sync_products.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_masters_loop.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_reset_sync.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)
if not exist "%DIST%\tally_invoice_loop.exe" (
  echo Executables not found. Building...
  call "%~dp0build_exe.bat"
)

if exist "%PACKAGE%" rmdir /s /q "%PACKAGE%"
mkdir "%PACKAGE%"

copy "%DIST%\tally_fetch.exe" "%PACKAGE%"
copy "%DIST%\tally_fetch_invoices.exe" "%PACKAGE%"
copy "%DIST%\tally_sync.exe" "%PACKAGE%"
copy "%DIST%\tally_loop.exe" "%PACKAGE%"
copy "%DIST%\tally_invoice_loop.exe" "%PACKAGE%"
copy "%DIST%\tally_ui.exe" "%PACKAGE%"
copy "%DIST%\tally_fetch_customers.exe" "%PACKAGE%"
copy "%DIST%\tally_sync_customers.exe" "%PACKAGE%"
copy "%DIST%\tally_fetch_products.exe" "%PACKAGE%"
copy "%DIST%\tally_sync_products.exe" "%PACKAGE%"
copy "%DIST%\tally_masters_loop.exe" "%PACKAGE%"
copy "%DIST%\tally_reset_sync.exe" "%PACKAGE%"
copy "%~dp0.env.example" "%PACKAGE%"
copy "%~dp0README.md" "%PACKAGE%"
copy "%~dp0requirements.txt" "%PACKAGE%"

if "%1"=="--include-env" (
  if exist "%~dp0.env" (
    copy "%~dp0.env" "%PACKAGE%"
  ) else (
    echo Note: .env not found, skipped.
  )
)

set ZIP=%RELEASE%\tally_middleware_release.zip
if exist "%ZIP%" del "%ZIP%"

powershell -Command "Compress-Archive -Path '%PACKAGE%\*' -DestinationPath '%ZIP%'"

echo Package created: %ZIP%
endlocal
