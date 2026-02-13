param(
    [string]$ExePath = "",
    [string]$EnvPath = "",
    [string]$TaskName = "TallyMiddlewareUI"
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
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
    Write-Host "Build it first with: .\build_exe.ps1"
    exit 1
}

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
New-Item -Path $runKey -Force | Out-Null
New-ItemProperty -Path $runKey -Name $TaskName -Value "`"$ExePath`"" -PropertyType String -Force | Out-Null

$envKey = "HKCU:\Environment"
New-Item -Path $envKey -Force | Out-Null
New-ItemProperty -Path $envKey -Name "TALLY_ENV_PATH" -Value $EnvPath -PropertyType String -Force | Out-Null

Write-Host "Autostart enabled for current user: $ExePath"
Write-Host "TALLY_ENV_PATH set to: $EnvPath"
