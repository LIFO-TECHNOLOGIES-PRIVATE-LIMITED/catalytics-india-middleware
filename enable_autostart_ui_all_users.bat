@echo off
setlocal

set ROOT=%~dp0..
set EXE=%ROOT%\dist\tally_ui.exe
set ENV=%~dp0.env
set TASKNAME=TallyMiddlewareUI

if not exist "%EXE%" (
  echo Executable not found: %EXE%
  echo Build it first with: .\tally_middleware\build_exe.bat
  exit /b 1
)

echo This requires Administrator privileges.
powershell -Command "New-Item -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' -Force | Out-Null; New-ItemProperty -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '%TASKNAME%' -Value '\"%EXE%\"' -PropertyType String -Force | Out-Null; New-Item -Path 'HKLM:\System\CurrentControlSet\Control\Session Manager\Environment' -Force | Out-Null; New-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Session Manager\Environment' -Name 'TALLY_ENV_PATH' -Value '%ENV%' -PropertyType String -Force | Out-Null"

echo Autostart enabled for ALL users.
echo TALLY_ENV_PATH set to %ENV%
endlocal
