@echo off
setlocal

set TASKNAME=TallyMiddlewareUI

powershell -Command "if (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '%TASKNAME%' -ErrorAction SilentlyContinue) { Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '%TASKNAME%' -Force }; if (Get-ItemProperty -Path 'HKCU:\Environment' -Name 'TALLY_ENV_PATH' -ErrorAction SilentlyContinue) { Remove-ItemProperty -Path 'HKCU:\Environment' -Name 'TALLY_ENV_PATH' -Force }"

echo Autostart disabled for current user.
echo TALLY_ENV_PATH removed.
endlocal
