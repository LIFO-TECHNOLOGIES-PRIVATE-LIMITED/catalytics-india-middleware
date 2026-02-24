@echo off
setlocal

set PYTHON_EXE=py

echo Building executables using PyInstaller...
%PYTHON_EXE% -m pip install --upgrade pyinstaller
%PYTHON_EXE% -m pip install -r tally_middleware\requirements.txt

%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch tally_middleware\fetch_tally.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch_invoices tally_middleware\fetch_invoices.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_sync tally_middleware\sync_catalytics.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_loop tally_middleware\run_loop.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_invoice_loop tally_middleware\run_loop_invoices.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_ui tally_middleware\web_ui.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch_customers tally_middleware\fetch_customers.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_sync_customers tally_middleware\sync_customers.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_fetch_products tally_middleware\fetch_products.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_sync_products tally_middleware\sync_products.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_masters_loop tally_middleware\run_loop_masters.py
%PYTHON_EXE% -m PyInstaller --onefile --clean --name tally_reset_sync tally_middleware\reset_sync.py

echo Executables created in .\dist (tally_fetch.exe, tally_fetch_invoices.exe, tally_sync.exe, tally_loop.exe, tally_invoice_loop.exe, tally_ui.exe, tally_fetch_customers.exe, tally_sync_customers.exe, tally_fetch_products.exe, tally_sync_products.exe, tally_masters_loop.exe, tally_reset_sync.exe)
endlocal
