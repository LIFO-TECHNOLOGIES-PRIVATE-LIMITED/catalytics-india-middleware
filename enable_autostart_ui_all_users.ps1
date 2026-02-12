param(
    [string]$ExePath = "",
    [string]$EnvPath = "",
    [string]$TaskName = "TallyMiddlewareUI"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$DefaultExe = Join-Path $Root "dist\tally_ui.exe"
$DefaultEnv = Join-Path $PSScriptRoot ".env"

if (-not $ExePath) {
    $ExePath = $DefaultExe
}
if (-not $EnvPath) {
    $EnvPath = $DefaultEnv
}

if (!(Test-Path $ExePath)) {
    Write-Host "Executable not found: $ExePath"
    Write-Host "Build it first with: .\tally_middleware\build_exe.ps1"
    exit 1
}

# Requires admin
$runKey = "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run"
New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name $TaskName -Value "`"$ExePath`"" -PropertyType String -Force | Out-Null

$envKey = "HKLM:\System\CurrentControlSet\Control\Session Manager\Environment"
New-Item -Path $envKey -Force | Out-Null
New-ItemProperty -Path $envKey -Name "TALLY_ENV_PATH" -Value $EnvPath -PropertyType String -Force | Out-Null

Write-Host "Autostart enabled for ALL users: $ExePath"
Write-Host "TALLY_ENV_PATH set to: $EnvPath"
