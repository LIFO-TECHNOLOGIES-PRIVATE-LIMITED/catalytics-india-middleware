@echo off
setlocal

set TASKNAME=TallyMiddlewareUI

echo This requires Administrator privileges.
powershell -Command "if (Get-ItemProperty -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '%TASKNAME%' -ErrorAction SilentlyContinue) { Remove-ItemProperty -Path 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '%TASKNAME%' -Force }; if (Get-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Session Manager\Environment' -Name 'TALLY_ENV_PATH' -ErrorAction SilentlyContinue) { Remove-ItemProperty -Path 'HKLM:\System\CurrentControlSet\Control\Session Manager\Environment' -Name 'TALLY_ENV_PATH' -Force }"

echo Autostart disabled for ALL users.
echo TALLY_ENV_PATH removed.
endlocal
