# Environment-Based Auto-Fetch and Auto-Sync Flow

## Overview

The Arasan Gas Middleware provides automated fetch and sync operations controlled by environment variables and managed through a web dashboard. The system runs continuous background loops that fetch data from Tally ERP and sync it to the Catalytics backend.

---

## Architecture Components

### 1. Configuration (`config.py`)
- Loads environment variables from `.env` file
- Supports multi-company Tally configuration
- Defines sync intervals and batch sizes
- Provides dynamic configuration reload capability

### 2. Automation Manager (`automation_manager.py`)
- Manages three independent background threads:
  - **Fetch Master Data** (customers + products)
  - **Fetch Invoices**
  - **Sync to Catalytics**
- Maintains state in `automation_state.json`
- Provides start/stop/restart controls
- Logs all operations to dashboard terminal

### 3. Web Dashboard (`dashboard.py`)
- Flask-based web interface on port 5000
- Real-time monitoring of sync status
- Manual trigger buttons for each operation
- Live terminal output display
- Statistics and error tracking

---

## Environment Variables

### Sync Intervals
```env
# How often to sync data to Catalytics (in seconds)
SYNC_INTERVAL_SECONDS=300  # Default: 5 minutes

# How often to fetch customers from Tally (in minutes)
FETCH_CUSTOMERS_INTERVAL_MINUTES=60  # Default: 1 hour

# How often to fetch products from Tally (in minutes)
FETCH_PRODUCTS_INTERVAL_MINUTES=60  # Default: 1 hour
```

### Invoice Fetch Configuration
```env
# Start date for invoice fetching (format: YYYYMMDD)
# If not set, fetches only today's invoices
INVOICE_FETCH_START_DATE=20240101  # Optional: fetch from specific date
```

### Multi-Company Configuration
```env
# Active companies (comma-separated keys)
TALLY_COMPANY_ACTIVE=arasan_gas,arasan_gas_2

# Company mappings
TALLY_COMPANY_arasan_gas=Arasan Gas Company 1
TALLY_COMPANY_arasan_gas_2=Arasan Gas Company 2
```

### Batch Processing
```env
# Number of records to sync in each batch
SYNC_BATCH_SIZE=50  # Default: 50 records per batch
```

---

## Auto-Fetch Flow

### 1. Fetch Master Data Loop
**Interval**: `FETCH_CUSTOMERS_INTERVAL_MINUTES` (default: 60 minutes)

**Process**:
1. Fetch customers from all active Tally companies
2. Fetch products from all active Tally companies
3. Save to SQLite with `is_synced=0`
4. Wait for next interval

**Files Involved**:
- `fetch_customers.py` → `fetch_customers_from_all_companies()`
- `fetch_products.py` → `fetch_products_from_all_companies()`
- `db.py` → Database operations

**What Gets Fetched**:
- **Customers**: Name, GSTIN, address, phone, email, city, state
- **Products**: Name, HSN code, unit, rate, description

### 2. Fetch Invoices Loop
**Interval**: `SYNC_INTERVAL_SECONDS` (default: 300 seconds = 5 minutes)

**Process**:
1. For each active company:
   - Fetch sales vouchers from Tally
   - Extract customer and product references
   - **Auto-save referenced customers to SQLite** (if not exists)
   - **Auto-save referenced products to SQLite** (if not exists)
   - Save invoice with full voucher JSON
2. All saved with `is_synced=0`
3. Wait for next interval

**Files Involved**:
- `fetch_invoices.py` → `fetch_invoices_from_all_companies()`
- `tally_client.py` → Tally API communication
- `db.py` → Database operations

**Date Range**:
- If `INVOICE_FETCH_START_DATE` is set: Fetches from that date to today
- If not set: Fetches only today's invoices

**Important**: This is where the race condition bug exists - invoices are saved before their referenced customers/products are synced to Catalytics.

---

## Auto-Sync Flow

### Sync Loop
**Interval**: `SYNC_INTERVAL_SECONDS` (default: 300 seconds = 5 minutes)

**Process**:
1. Sync customers (batch by batch)
2. Sync products (batch by batch)
3. Sync invoices as delivery challans (batch by batch)
4. Wait for next interval

**Files Involved**:
- `sync_to_catalytics.py` → `CatalyticsSyncer.sync_all()`

### Current Sync Methods

#### 1. Customer Sync
**Method**: `sync_customers()`
**Endpoint**: `/import/tally-customer-payload/`
**Payload**: Full customer data with ledger details

#### 2. Product Sync
**Method**: `sync_products()`
**Endpoint**: `/import/tally-product-name-payload/`
**Payload**: Product name (auto-creates in Catalytics)
**Note**: Saves full Tally name in `Product.short_name` field

#### 3. Invoice Sync (Current - Full Payload)
**Method**: `sync_invoices_to_dc()` ← **Currently Used**
**Endpoint**: `/import/tally-dc-payload/`
**Payload**: Full voucher + ledgers + stock_items
**Issue**: Requires customers/products to be synced first (race condition)

#### 4. Invoice Sync (New - Lightweight)
**Method**: `sync_invoices_simple()` ← **Available but NOT Used**
**Endpoint**: `/import/tally-dc-name-payload/`
**Payload**: Only voucher data (no ledgers, no stock_items)
**Benefit**: Auto-creates customers/products by name matching

---

## Current Sync Method Usage

### In `sync_all()` method:
```python
def sync_all(self):
    results = {
        'customers': self.sync_customers(),
        'products': self.sync_products(),
        'invoices': self.sync_invoices_to_dc()  # ← Uses FULL payload method
    }
    return results
```

### In Dashboard Manual Triggers:
- **Sync All**: Calls `sync_all()` → Uses `sync_invoices_to_dc()`
- **Sync Invoices**: Calls `sync_invoices_to_dc()` directly

### In Automation Manager:
```python
def _sync_loop(self):
    syncer = CatalyticsSyncer()
    ok, _ = self._run_with_log_capture(syncer.sync_all)  # ← Uses sync_invoices_to_dc()
```

---

## Race Condition Issue

### Current Flow (Problematic):
```
1. Fetch Invoice → Auto-save customer/product to SQLite (is_synced=0)
2. Save invoice to SQLite (is_synced=0)
3. [Later] Sync customers → Mark as synced
4. [Later] Sync products → Mark as synced
5. [Later] Sync invoice → FAILS if customer/product not synced yet
```

### Why It Fails:
The `sync_invoices_to_dc()` method requires:
- Customer must exist in Catalytics (validated by ID)
- Product must exist in Catalytics (validated by ID)
- If either is missing → Sync fails with validation error

### Solution Options:

#### Option 1: Fix Race Condition (Original Bugfix Spec)
Modify `fetch_invoices.py` to immediately sync customers/products before saving invoice:
```python
# After auto-saving customer/product to SQLite
syncer = CatalyticsSyncer()
syncer.sync_customers()  # Sync immediately
syncer.sync_products()   # Sync immediately
# Then save invoice
```

#### Option 2: Use Lightweight API (Already Implemented)
Switch from `sync_invoices_to_dc()` to `sync_invoices_simple()`:
- No validation required
- Auto-creates customers/products by name
- Matches using `Product.short_name` field
- Already implemented and tested

---

## How to Switch to Lightweight API

### Step 1: Update `sync_to_catalytics.py`
Change the `sync_all()` method:
```python
def sync_all(self):
    results = {
        'customers': self.sync_customers(),
        'products': self.sync_products(),
        'invoices': self.sync_invoices_simple()  # ← Change this line
    }
    return results
```

### Step 2: Update Dashboard Triggers (Optional)
If you want manual triggers to use the new API:

In `dashboard.py`, change:
```python
@app.route('/api/trigger/sync_invoices', methods=['POST'])
def trigger_sync_invoices():
    syncer = CatalyticsSyncer()
    success, output = _run_with_dashboard_capture(syncer.sync_invoices_simple)  # ← Change
    # ... rest of code
```

### Step 3: Restart Automation
After making changes:
1. Stop the middleware
2. Restart the middleware
3. Automation will use the new sync method

---

## Monitoring and Control

### Web Dashboard
**URL**: `http://localhost:5000`

**Features**:
- Real-time status of Tally and Catalytics connections
- Statistics: Total/synced/unsynced counts for customers, products, invoices
- Manual trigger buttons for each operation
- Live terminal output
- Activity log and error log
- Data tables with sync status

### Manual Triggers
- **Fetch Customers**: Immediate customer fetch from Tally
- **Fetch Products**: Immediate product fetch from Tally
- **Fetch Invoices**: Background invoice fetch (returns immediately)
- **Sync All**: Sync customers, products, and invoices
- **Sync Customers**: Sync only customers
- **Sync Products**: Sync only products
- **Sync Invoices**: Sync only invoices

### Automation Controls
- **Start**: Begin automated loops
- **Stop**: Stop all automation threads
- **Restart**: Stop and start automation

### State Persistence
State is saved in `automation_state.json`:
```json
{
  "status": "running",
  "intervals": {
    "fetch_master": 3600,
    "fetch_invoices": 300,
    "sync": 300
  },
  "last_runs": {
    "fetch_master": "2024-03-02T10:30:00",
    "fetch_invoices": "2024-03-02T10:35:00",
    "sync": "2024-03-02T10:35:00"
  },
  "next_runs": {
    "fetch_master": "2024-03-02T11:30:00",
    "fetch_invoices": "2024-03-02T10:40:00",
    "sync": "2024-03-02T10:40:00"
  }
}
```

---

## Logging

### Log Files
Located in `logs/` directory:
- `arasan_gas.log` - Main application log
- `fetch_master_data.log` - Customer/product fetch log
- `fetch_invoices.log` - Invoice fetch log
- `sync_to_catalytics.log` - Sync operations log

### Dashboard Terminal
Real-time log output displayed in web dashboard:
- All fetch operations
- All sync operations
- Errors and warnings
- Progress indicators

---

## Database Schema

### SQLite Tables

#### customers
```sql
- id (primary key)
- name, tally_company, tally_guid
- gstin, pan, address, city, state, pincode, phone, email
- is_synced (0 or 1)
- catalytics_id (after sync)
- sync_attempts, last_sync_error
- first_fetched_at, last_updated_at, last_sync_at
- data_json (raw Tally data)
- last_response_json (API response)
```

#### products
```sql
- id (primary key)
- name, tally_company, tally_guid
- hsn_code, unit, rate, description
- is_synced (0 or 1)
- catalytics_id (after sync)
- sync_attempts, last_sync_error
- first_fetched_at, last_updated_at, last_sync_at
- data_json (raw Tally data)
- last_response_json (API response)
```

#### invoices
```sql
- id (primary key)
- tally_voucher_no, tally_company, tally_guid
- voucher_date, customer_name, customer_guid
- billing_address, delivery_address
- total_amount, tax_amount, items_json
- dc_no (after sync)
- is_synced (0 or 1)
- catalytics_dc_id (after sync)
- sync_attempts, last_sync_error
- first_fetched_at, last_updated_at, last_sync_at
- data_json (raw Tally voucher JSON)
- last_response_json (API response)
```

---

## Summary

### Current State
✅ Auto-fetch is working correctly
✅ Auto-sync is working correctly
✅ Environment-based configuration is working
⚠️ Race condition exists in invoice sync (using full payload method)
✅ Lightweight API is implemented but not used

### Recommendation
Switch to `sync_invoices_simple()` method to:
- Eliminate race condition
- Simplify sync process
- Auto-create missing customers/products
- Improve reliability

### Next Steps
1. Update `sync_all()` to use `sync_invoices_simple()`
2. Test the new flow
3. Monitor sync success rate
4. Consider deprecating the full payload method
