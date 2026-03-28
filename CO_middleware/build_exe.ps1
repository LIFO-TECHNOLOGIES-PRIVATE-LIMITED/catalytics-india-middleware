$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

& (Join-Path $Root "build_exe.bat")
exit $LASTEXITCODE
