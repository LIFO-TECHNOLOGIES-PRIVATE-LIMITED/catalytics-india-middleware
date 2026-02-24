@echo off
setlocal

set ROOT=%~dp0
set EXE=%ROOT%\dist\tally_ui.exe
set ENV=%~dp0.env
set TASKNAME=TallyMiddlewareUI

if not exist "%EXE%" (
  echo Executable not found: %EXE%
  echo Build it first with: .\build_exe.bat
  exit /b 1
)

powershell -Command "New-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Force | Out-Null; New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '%TASKNAME%' -Value '\"%EXE%\"' -PropertyType String -Force | Out-Null; New-Item -Path 'HKCU:\Environment' -Force | Out-Null; New-ItemProperty -Path 'HKCU:\Environment' -Name 'TALLY_ENV_PATH' -Value '%ENV%' -PropertyType String -Force | Out-Null"

echo Autostart enabled for current user.
echo TALLY_ENV_PATH set to %ENV%
endlocal
