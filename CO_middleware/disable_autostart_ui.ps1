param(
    [string]$TaskName = "TallyMiddlewareUI"
)

$ErrorActionPreference = "Stop"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

if (Get-ItemProperty -Path $runKey -Name $TaskName -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $runKey -Name $TaskName -Force
    Write-Host "Autostart disabled for current user."
} else {
    Write-Host "Autostart entry not found."
}

$envKey = "HKCU:\Environment"
if (Get-ItemProperty -Path $envKey -Name "TALLY_ENV_PATH" -ErrorAction SilentlyContinue) {
    Remove-ItemProperty -Path $envKey -Name "TALLY_ENV_PATH" -Force
    Write-Host "TALLY_ENV_PATH removed."
}
