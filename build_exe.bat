@echo off
setlocal

set PYTHON_EXE=py
pushd "%~dp0"

echo Building executables using PyInstaller...
%PYTHON_EXE% -m pip install --upgrade pyinstaller
%PYTHON_EXE% -m pip install -r requirements.txt

%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch fetch_tally.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch_invoices fetch_invoices.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_sync sync_catalytics.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_loop run_loop.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_invoice_loop run_loop_invoices.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --noconsole --name tally_ui web_ui.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch_customers fetch_customers.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_sync_customers sync_customers.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch_products fetch_products.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_sync_products sync_products.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_masters_loop run_loop_masters.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_reset_sync reset_sync.py

echo Executables created in .\dist (tally_fetch.exe, tally_fetch_invoices.exe, tally_sync.exe, tally_loop.exe, tally_invoice_loop.exe, tally_ui.exe, tally_fetch_customers.exe, tally_sync_customers.exe, tally_fetch_products.exe, tally_sync_products.exe, tally_masters_loop.exe, tally_reset_sync.exe)
popd
endlocal
