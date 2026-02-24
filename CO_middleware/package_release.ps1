param(
    [switch]$IncludeEnv
)

$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
Set-Location $Root

$distDir = Join-Path $Root "dist"
$fetchExe = Join-Path $distDir "tally_fetch.exe"
$fetchInvoicesExe = Join-Path $distDir "tally_fetch_invoices.exe"
$syncExe = Join-Path $distDir "tally_sync.exe"
$loopExe = Join-Path $distDir "tally_loop.exe"
$invoiceLoopExe = Join-Path $distDir "tally_invoice_loop.exe"
$uiExe = Join-Path $distDir "tally_ui.exe"
$fetchCustomersExe = Join-Path $distDir "tally_fetch_customers.exe"
$syncCustomersExe = Join-Path $distDir "tally_sync_customers.exe"
$fetchProductsExe = Join-Path $distDir "tally_fetch_products.exe"
$syncProductsExe = Join-Path $distDir "tally_sync_products.exe"
$mastersLoopExe = Join-Path $distDir "tally_masters_loop.exe"
$resetSyncExe = Join-Path $distDir "tally_reset_sync.exe"

if (!(Test-Path $fetchExe) -or !(Test-Path $fetchInvoicesExe) -or !(Test-Path $syncExe) -or !(Test-Path $loopExe) -or !(Test-Path $invoiceLoopExe) -or !(Test-Path $uiExe) -or !(Test-Path $fetchCustomersExe) -or !(Test-Path $syncCustomersExe) -or !(Test-Path $fetchProductsExe) -or !(Test-Path $syncProductsExe) -or !(Test-Path $mastersLoopExe) -or !(Test-Path $resetSyncExe)) {
    Write-Host "Executables not found. Building with build_exe.ps1..."
    & (Join-Path $PSScriptRoot "build_exe.ps1")
}

$releaseDir = Join-Path $PSScriptRoot "release"
$packageDir = Join-Path $releaseDir "package"
if (Test-Path $packageDir) {
    Remove-Item $packageDir -Recurse -Force
}
New-Item -ItemType Directory -Force $packageDir | Out-Null

Copy-Item $fetchExe $packageDir
Copy-Item $fetchInvoicesExe $packageDir
Copy-Item $syncExe $packageDir
Copy-Item $loopExe $packageDir
Copy-Item $invoiceLoopExe $packageDir
Copy-Item $uiExe $packageDir
Copy-Item $fetchCustomersExe $packageDir
Copy-Item $syncCustomersExe $packageDir
Copy-Item $fetchProductsExe $packageDir
Copy-Item $syncProductsExe $packageDir
Copy-Item $mastersLoopExe $packageDir
Copy-Item $resetSyncExe $packageDir

Copy-Item (Join-Path $PSScriptRoot ".env.example") $packageDir
Copy-Item (Join-Path $PSScriptRoot "README.md") $packageDir
Copy-Item (Join-Path $PSScriptRoot "requirements.txt") $packageDir

if ($IncludeEnv) {
    $envPath = Join-Path $PSScriptRoot ".env"
    if (Test-Path $envPath) {
        Copy-Item $envPath $packageDir
    } else {
        Write-Host "Note: .env not found, skipped."
    }
}

$zipPath = Join-Path $releaseDir "tally_middleware_release.zip"
if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

Compress-Archive -Path (Join-Path $packageDir "*") -DestinationPath $zipPath

Write-Host "Package created: $zipPath"
