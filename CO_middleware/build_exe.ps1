param(
    [string]$PythonExe = "py",
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
Set-Location $Root

Write-Host "Building executables using PyInstaller..."
Write-Host "Python: $PythonExe"

& $PythonExe -m pip install --upgrade pyinstaller
& $PythonExe -m pip install -r requirements.txt

& $PythonExe -m PyInstaller --onefile --clean --name tally_fetch fetch_tally.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_fetch_invoices fetch_invoices.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_sync sync_catalytics.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_loop run_loop.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_invoice_loop run_loop_invoices.py
& $PythonExe -m PyInstaller --onefile --clean --noconsole --name tally_ui web_ui.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_fetch_customers fetch_customers.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_sync_customers sync_customers.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_fetch_products fetch_products.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_sync_products sync_products.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_masters_loop run_loop_masters.py
& $PythonExe -m PyInstaller --onefile --clean --name tally_reset_sync reset_sync.py

Write-Host "Executables created in .\dist (tally_fetch.exe, tally_fetch_invoices.exe, tally_sync.exe, tally_loop.exe, tally_invoice_loop.exe, tally_ui.exe, tally_fetch_customers.exe, tally_sync_customers.exe, tally_fetch_products.exe, tally_sync_products.exe, tally_masters_loop.exe, tally_reset_sync.exe)"
