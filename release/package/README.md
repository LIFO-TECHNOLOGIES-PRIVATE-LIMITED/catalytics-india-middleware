# Tally Delivery Challan Middleware

Standalone scripts:
- `fetch_tally.py`: Pull delivery notes, ledgers, and stock items from Tally and store into SQLite (DC flow).
- `fetch_invoices.py`: Pull sales invoices from Tally and stage them as DC-ready records in SQLite (invoice flow).
- `sync_catalytics.py`: Push unsynced delivery notes from SQLite to Catalytics via API.
- `fetch_customers.py`: Pull customer ledgers from Tally and store into SQLite.
- `sync_customers.py`: Push unsynced ledgers from SQLite to Catalytics.
- `fetch_products.py`: Pull stock items from Tally and store into SQLite.
- `sync_products.py`: Push unsynced stock items from SQLite to Catalytics.

## Database Schema
SQLite tables (created automatically):
- `companies`
- `ledgers`
- `stock_items`
- `delivery_notes`
- `delivery_note_items`
- `sync_status`
- `ledger_sync_status`
- `stock_sync_status`
- `sync_runs`

## Fetch From Tally
```bash
python fetch_tally.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --tally-url http://localhost:9000/ \
  --company "Your Tally Company" \
  --entity-id 25 \
  --from-date 20240101 \
  --to-date 20240204 \
  --fetch-stock
```

If `--from-date`/`--to-date` are omitted, it defaults to the current financial year range.

## Fetch Sales Invoices (Invoice -> DC Flow)
```bash
python fetch_invoices.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --tally-url http://localhost:9000/ \
  --company "Your Tally Company" \
  --entity-id 25 \
  --from-date 20240101 \
  --to-date 20240204 \
  --dc-prefix INV-
```

## Sync To Catalytics
```bash
python sync_catalytics.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --api-base-url http://localhost:8000 \
  --api-key YOUR_API_KEY \
  --entity-id 25 \
  --batch-size 10
```

## Fetch Customers (Ledger Master)
```bash
python fetch_customers.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --tally-url http://localhost:9000/ \
  --company "Your Tally Company" \
  --entity-id 25 \
  --fetch-full
```

## Sync Customers
```bash
python sync_customers.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --api-base-url http://localhost:8000 \
  --api-key YOUR_API_KEY \
  --entity-id 25 \
  --batch-size 50
```

## Fetch Products (Stock Master)
```bash
python fetch_products.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --tally-url http://localhost:9000/ \
  --company "Your Tally Company" \
  --entity-id 25 \
  --fetch-full
```

## Sync Products
```bash
python sync_products.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --api-base-url http://localhost:8000 \
  --api-key YOUR_API_KEY \
  --entity-id 25 \
  --batch-size 50
```

## Reset Sync Flags (Force Re-sync)
```bash
python reset_sync.py --db-path C:\path\to\tally_dc.sqlite --reset-type products
python reset_sync.py --db-path C:\path\to\tally_dc.sqlite --reset-type customers
python reset_sync.py --db-path C:\path\to\tally_dc.sqlite --reset-type dc
python reset_sync.py --db-path C:\path\to\tally_dc.sqlite --reset-type all
```

## Run Loop (Polling)
```bash
python run_loop.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --tally-url http://localhost:9000/ \
  --company "Your Tally Company" \
  --entity-id 25 \
  --api-base-url http://localhost:8000 \
  --api-key YOUR_API_KEY \
  --interval 300
```

Use `--once` for a single run.

Invoice loop variant:
```bash
python run_loop_invoices.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --tally-url http://localhost:9000/ \
  --company "Your Tally Company" \
  --entity-id 25 \
  --api-base-url http://localhost:8000 \
  --api-key YOUR_API_KEY \
  --dc-prefix INV- \
  --interval 300
```

## Run Master Loop (Customers/Products)
```bash
python run_loop_masters.py \
  --db-path C:\path\to\tally_dc.sqlite \
  --tally-url http://localhost:9000/ \
  --company "Your Tally Company" \
  --entity-id 25 \
  --api-base-url http://localhost:8000 \
  --api-key YOUR_API_KEY \
  --interval 3600 \
  --mode both
```

Use `--mode customers` or `--mode products` and `--once` for a single run.

## Config via .env
Provide a `.env` file with:
```
TALLY_DB_PATH=C:\path\to\tally_dc.sqlite
TALLY_URL=http://localhost:9000/
TALLY_COMPANY=Your Tally Company
CATALYTICS_ENTITY_ID=25
CATALYTICS_API_BASE_URL=http://localhost:8000
CATALYTICS_API_KEY=YOUR_API_KEY
TALLY_FETCH_STOCK=true
TALLY_INVOICE_DC_PREFIX=INV-
TALLY_FETCH_FULL_CUSTOMERS=false
TALLY_FETCH_FULL_PRODUCTS=false
TALLY_FETCH_FULL_PRODUCTS_AUTO_HSN=true
TALLY_FETCH_FULL_PRODUCTS_AUTO_CODE=true
AUTO_SYNC_CUSTOMERS=false
AUTO_SYNC_PRODUCTS=false
SYNC_BATCH_SIZE=10
LOG_LEVEL=INFO
LOG_JSON=false
```

Then run with `--config path\to\.env`.

`fetch_tally.py`, `fetch_invoices.py`, `sync_catalytics.py`, `run_loop.py`, and `run_loop_invoices.py` will also auto-load `.env` if present.

Sample file: `.env.example`

## Logging
- Set `LOG_JSON=true` for JSON logs.
- Set `LOG_LEVEL=DEBUG|INFO|WARNING|ERROR`.

## Dry Run
- Fetch: `--dry-run` (no DB commit)
- Sync: `--dry-run` (no API call)

## Endpoints Used
DC sync endpoint:
`POST /import/tally-delivery-challan-payload/`
Payload:
- `entity_id` or `company_name`
- `voucher` or `vouchers[]`
- `ledgers` map (optional)
- `stock_items` map (optional)

Customer sync endpoint:
`POST /import/tally-customer-payload/`
Payload:
- `entity_id` or `company_name`
- `ledger` or `ledgers[]`

Product sync endpoint:
`POST /import/tally-product-payload/`
Payload:
- `entity_id` or `company_name`
- `stock_item` or `stock_items[]`

## Tests
```bash
python -m unittest discover -s tests -p "test_*.py"
```

## Build .exe (Windows)
```bat
cd C:\Github\catalytics-india-middleware
.\build_exe.bat
```

Or with PowerShell:
```powershell
cd C:\Github\catalytics-india-middleware
.\build_exe.ps1
```

Executables are created in `dist\`. The UI executable is `dist\tally_ui.exe`.
Invoice binaries:
`dist\tally_fetch_invoices.exe`, `dist\tally_invoice_loop.exe`.
New customer/product binaries:
`dist\tally_fetch_customers.exe`, `dist\tally_sync_customers.exe`, `dist\tally_fetch_products.exe`, `dist\tally_sync_products.exe`.
Master loop binary:
`dist\tally_masters_loop.exe`.
Reset sync binary:
`dist\tally_reset_sync.exe`.

## Web UI (Flask)
Run the web UI server:
```bat
pip install -r requirements.txt
py web_ui.py
```

Defaults:
- Host: `0.0.0.0`
- Port: `8787`
- Auto-start loop: `false`

Configure via `.env`:
```
UI_HOST=0.0.0.0
UI_PORT=8787
UI_API_KEY=
UI_AUTO_START=false
AUTO_SYNC_CUSTOMERS=false
AUTO_SYNC_PRODUCTS=false
LOG_FILE=C:\Github\catalytics-india-middleware\logs\app.log
```

If `UI_API_KEY` is set, pass `X-API-Key` in requests.

DC source selection in UI:
- Use the `DC Source` card to switch between `Delivery Note -> DC` and `Sales Invoice -> DC`.
- For invoice mode, set prefix (example `INV-`) and click `Apply`.
- This change applies immediately to UI fetch/loop runs.
- Persistent default still comes from `.env`:
  - `TALLY_SOURCE_DOC=delivery_note` or `sales_invoice`
  - `TALLY_INVOICE_DC_PREFIX=INV-`

Diagnostics in UI:
- Use `Diagnostics -> Analyze Now` to quickly inspect failed sync reasons.
- The panel shows:
  - Missing env/config keys
  - Tally/API connectivity status
  - Pending/failed queue counts
  - Top error patterns with fix hints
- Use it before retrying failed records in production.

## Auto-start UI on Windows login
Enable:
```bat
.\enable_autostart_ui.bat
```

Disable:
```bat
.\disable_autostart_ui.bat
```

PowerShell:
```powershell
.\enable_autostart_ui.ps1
.\disable_autostart_ui.ps1
```

These scripts also set `TALLY_ENV_PATH` to `.env`.
If you need a custom `.env` location, set `TALLY_ENV_PATH` in Windows Environment variables.

### Auto-start for ALL users (admin required)
```bat
.\enable_autostart_ui_all_users.bat
.\disable_autostart_ui_all_users.bat
```

PowerShell (run as Administrator):
```powershell
.\enable_autostart_ui_all_users.ps1
.\disable_autostart_ui_all_users.ps1
```

## Package .exe + env into zip
Batch:
```bat
cd C:\Github\catalytics-india-middleware
.\package_release.bat
```

Include `.env` (if present):
```bat
.\package_release.bat --include-env
```

PowerShell:
```powershell
cd C:\Github\catalytics-india-middleware
.\package_release.ps1
```

Include `.env`:
```powershell
.\package_release.ps1 -IncludeEnv
```


