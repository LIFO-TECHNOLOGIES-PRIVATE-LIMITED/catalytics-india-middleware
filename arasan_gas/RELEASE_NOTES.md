# Arasan Gas Middleware - Release Notes

## Version 1.2.0 - Customer Table Fix & Diagnostics (March 3, 2026)

### Critical Fixes
- **Fixed Customer table case-sensitivity issue** - Resolved PostgreSQL table name mismatch (`master.Customer` → `master.customer`)
- **Added database diagnostic tools** - Scripts to check and fix table issues automatically
- **Improved error handling** - Better error messages and recovery for sync failures

### New Diagnostic Tools
- `check_pending_invoices.py` - View all pending invoices and their sync status
- `reset_failed_invoices.py` - Reset failed invoices to allow retry
- `test_single_invoice_sync.py` - Test syncing a single invoice for debugging
- `fix_customer_table_name.py` - Automatically detect and fix Customer table name issues
- `check_customer_table.py` - Verify Customer table exists and is accessible

### Backend Updates
- Customer model `db_table` fixed to lowercase (`master.customer`)
- Migration `0016_alter_customer_table.py` created and applied
- Improved error logging in `tally_dc_simple.py`
- Added table existence checks before queries

### Documentation
- `FIX_CUSTOMER_TABLE_CASE.md` - Customer table fix documentation
- Updated troubleshooting guides with table name issues
- Added diagnostic tool usage instructions

### Deployment Notes
- **IMPORTANT**: Django server must be completely restarted after deployment
- Run `fix_customer_table_name.py` if table name issues persist after restart
- Use diagnostic tools to troubleshoot sync failures before contacting support
- Check Django server logs for detailed error messages

---

## Version 1.1.0 - Lightweight DC Sync API (March 3, 2026)

### Release Date
Generated: Auto-timestamped during build

---

## What's New

### 1. Lightweight DC Sync API (Name-Based Matching)
- **New Endpoint**: `/import/tally-dc-name-payload/`
- **Auto-creates customers and products** if not found in Catalytics
- **Matches by name** instead of requiring pre-synced IDs
- **Eliminates race condition** where invoices fail to sync due to missing master data
- **Simplified payload** - sends only voucher data (no ledgers, no stock_items)

### 2. Product Matching Improvements
- **Primary Match Field**: `Product.short_name` (stores full Tally product name)
- **5 Matching Strategies**:
  1. Exact match on `Product.short_name` (full Tally name)
  2. Fuzzy match on `Product.short_name` (contains)
  3. Exact match on `Product.name` (product master only)
  4. Product Master + Variant match (parsed)
  5. Fuzzy match on `Product.name` (contains - last resort)

### 3. Updated Product Sync
- **Saves full Tally name** in `Product.short_name` field
- **Endpoint**: `/import/tally-product-name-payload/`
- **Auto-creates products** with minimal data if not found

### 4. Automatic Initialization
- **Logs folder** auto-created on first run
- **SQLite database** auto-created with all tables
- **No manual setup required** - just configure `.env` and run

---

## Key Features

### Environment-Based Configuration
All settings controlled via `.env` file:
- Multi-company Tally support
- Configurable sync intervals
- Invoice fetch date range
- Batch sizes and worker counts
- Auto-start automation on launch

### Automated Operations
- **Auto-fetch** customers, products, and invoices from Tally
- **Auto-sync** to Catalytics backend
- **Configurable intervals** for each operation
- **Background threads** for continuous operation

### Web Dashboard
- **Real-time monitoring** of sync status
- **Manual trigger buttons** for each operation
- **Live terminal output** display
- **Statistics and error tracking**
- **Data tables** with sync status

### Reliability Features
- **WAL mode** for SQLite (prevents "database is locked" errors)
- **Retry logic** with exponential backoff
- **Error logging** and tracking
- **State persistence** across restarts

---

## Installation & Setup

### 1. Extract Release Package
Unzip the release package to your desired location:
```
arasan_gas_client_release_YYYYMMDD_HHMM/
├── arasan_gas_dashboard.exe
├── .env
├── .env.example
├── logs/ (empty folder)
├── Start_Dashboard.bat
├── Install_AutoStart.bat
├── Remove_AutoStart.bat
└── README_CLIENT_SETUP.txt
```

### 2. Configure Environment
Edit `.env` file with your settings:

```env
# Entity Configuration
ENTITY_NAME=Arasan Gas
ENTITY_ID=1

# Tally Configuration
TALLY_URL=http://localhost:9000
TALLY_COMPANY_ACTIVE=arasan_gas
TALLY_COMPANY_arasan_gas=Arasan Gas Company Name

# Catalytics Backend
CATALYTICS_API_BASE=http://your-backend-url/api
POSTGRES_HOST=your-postgres-host
POSTGRES_PORT=5432
POSTGRES_DB=catalytics_db
POSTGRES_USER=your_user
POSTGRES_PASSWORD=your_password

# Sync Intervals
SYNC_INTERVAL_SECONDS=300
FETCH_CUSTOMERS_INTERVAL_MINUTES=60
FETCH_PRODUCTS_INTERVAL_MINUTES=60

# Invoice Fetch Configuration
INVOICE_FETCH_START_DATE=20240101

# Automation
AUTO_START_AUTOMATION=true
```

### 3. Launch Dashboard
**Option 1**: Double-click `arasan_gas_dashboard.exe`
**Option 2**: Run `Start_Dashboard.bat`

The dashboard will:
- Auto-create `logs/` folder if missing
- Auto-create `arasan_gas.sqlite` database if missing
- Auto-create all required database tables
- Start web server on `http://localhost:5000`
- Auto-open browser (if configured)
- Auto-start automation (if configured)

### 4. Access Dashboard
Open browser and navigate to: `http://localhost:5000`

---

## What Gets Auto-Created

### On First Run
1. **logs/** folder
   - `arasan_gas.log` - Main application log
   - `fetch_master_data.log` - Customer/product fetch log
   - `fetch_invoices.log` - Invoice fetch log
   - `sync_to_catalytics.log` - Sync operations log

2. **arasan_gas.sqlite** database with tables:
   - `customers` - Customer master data
   - `products` - Product master data
   - `invoices` - Invoice/DC data
   - `sync_status` - Sync operation tracking
   - `duplicate_log` - Duplicate detection audit

3. **automation_state.json** - Automation state persistence

---

## Sync Flow

### Current Flow (with Lightweight API)
```
1. Fetch Customers → Save to SQLite (is_synced=0)
2. Fetch Products → Save to SQLite (is_synced=0)
3. Fetch Invoices → Auto-save referenced customers/products → Save invoice
4. Sync Customers → Match/create in Catalytics → Mark as synced
5. Sync Products → Match/create in Catalytics → Mark as synced
6. Sync Invoices → Match/create customers & products by name → Create DC
```

### Benefits
- **No race condition** - customers/products auto-created during invoice sync
- **Simplified payload** - only voucher data sent
- **Better reliability** - name-based matching is more forgiving
- **Faster sync** - no need to fetch full ledgers and stock items

---

## API Endpoints Used

### Middleware → Catalytics Backend

1. **Customer Sync**
   - Endpoint: `POST /import/tally-customer-payload/`
   - Payload: Full customer ledger data

2. **Product Sync**
   - Endpoint: `POST /import/tally-product-name-payload/`
   - Payload: Product name with parsed fields
   - Auto-creates: Product master, variant, unit

3. **Invoice Sync (NEW - Lightweight)**
   - Endpoint: `POST /import/tally-dc-name-payload/`
   - Payload: Voucher data only
   - Auto-creates: Customers and products by name matching

---

## Configuration Options

### Sync Intervals
```env
# How often to sync data (in seconds)
SYNC_INTERVAL_SECONDS=300  # Default: 5 minutes

# How often to fetch master data (in minutes)
FETCH_CUSTOMERS_INTERVAL_MINUTES=60  # Default: 1 hour
FETCH_PRODUCTS_INTERVAL_MINUTES=60   # Default: 1 hour
```

### Invoice Fetch
```env
# Start date for invoice fetching (YYYYMMDD format)
# If not set, fetches only today's invoices
INVOICE_FETCH_START_DATE=20240101  # Optional
```

### Automation
```env
# Auto-start automation when dashboard launches
AUTO_START_AUTOMATION=true  # Default: true for EXE, false for dev

# Auto-register Windows startup (run on system boot)
AUTO_REGISTER_WINDOWS_STARTUP=false  # Default: false

# Auto-open browser when dashboard starts
AUTO_OPEN_BROWSER=true  # Default: true for EXE, false for dev
```

### Batch Processing
```env
# Number of records to sync in each batch
SYNC_BATCH_SIZE=50  # Default: 50
```

---

## Troubleshooting

### Database Locked Error
- **Solution**: Already fixed with WAL mode
- Database uses Write-Ahead Logging for concurrent access

### Sync Failures
- Check `.env` configuration
- Verify Tally server is running
- Verify Catalytics backend is accessible
- Check logs in `logs/` folder
- View errors in dashboard

### Missing Data
- Verify company names in `.env` match Tally exactly
- Check `TALLY_COMPANY_ACTIVE` includes all companies
- Verify date range in `INVOICE_FETCH_START_DATE`

### Automation Not Starting
- Check `AUTO_START_AUTOMATION=true` in `.env`
- Manually start from dashboard "Automation" section
- Check logs for errors

---

## Build Information

### Build Process
```batch
# Build new release
cd C:\Github\catalytics-india-middleware\arasan_gas
build_release.bat
```

### Output Location
```
C:\Github\catalytics-india-middleware\arasan_gas\release\
└── arasan_gas_client_release_YYYYMMDD_HHMM\
    └── arasan_gas_client_release_YYYYMMDD_HHMM.zip
```

### What's Included
- `arasan_gas_dashboard.exe` - Main executable (no console window)
- `.env` - Pre-configured environment file
- `.env.example` - Example configuration
- `logs/` - Empty logs folder
- `Start_Dashboard.bat` - Launch script
- `Install_AutoStart.bat` - Windows startup registration
- `Remove_AutoStart.bat` - Remove startup registration
- `README_CLIENT_SETUP.txt` - Setup instructions

---

## Technical Details

### Database Schema
- **SQLite** with WAL mode
- **Auto-created** on first run
- **Tables**: customers, products, invoices, sync_status, duplicate_log
- **Indexes**: Optimized for sync operations

### Logging
- **Rotating logs** with size limits
- **Separate logs** for each operation
- **Dashboard terminal** for real-time output
- **Error tracking** with timestamps

### Threading
- **3 background threads**:
  1. Fetch Master Data (customers + products)
  2. Fetch Invoices
  3. Sync to Catalytics
- **Thread-safe** operations with locks
- **Graceful shutdown** on stop

---

## Support

For issues or questions:
1. Check logs in `logs/` folder
2. Review dashboard error panel
3. Verify `.env` configuration
4. Check Tally and Catalytics connectivity
5. Contact support team

---

## Changelog

### Latest Release
- ✅ Implemented lightweight DC sync API with name-based matching
- ✅ Updated product sync to save full Tally name in `short_name`
- ✅ Added auto-creation for customers and products during invoice sync
- ✅ Improved product matching with 5 strategies
- ✅ Eliminated race condition in invoice sync
- ✅ Enhanced logging and error tracking
- ✅ Auto-initialization of logs and database

### Previous Versions
- Initial release with full payload sync
- Multi-company support
- Automated fetch and sync
- Web dashboard interface
