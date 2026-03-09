# CO Middleware - Complete Guide

## Table of Contents
1. [Overview](#overview)
2. [Architecture](#architecture)
3. [How It Works](#how-it-works)
4. [Setup & Configuration](#setup--configuration)
5. [Running the Middleware](#running-the-middleware)
6. [Code Flow Explained](#code-flow-explained)
7. [Troubleshooting](#troubleshooting)
8. [Testing](#testing)

---

## Overview

CO Middleware is a **single-company** Tally integration that:
- Fetches **Customers**, **Products**, and **Delivery Challans (DCs)** from Tally
- Stores them in a local SQLite database
- Syncs them to Catalytics backend API
- Provides a web dashboard for monitoring and manual control

**Key Features:**
- ✅ Automatic fetch and sync (configurable intervals)
- ✅ Web dashboard with real-time monitoring
- ✅ Tally crash prevention (10-second cooldown between requests)
- ✅ Manual trigger buttons for immediate fetch/sync
- ✅ Single company mode (simpler than multi-company)

---

## Architecture

```
┌─────────────────┐
│  Tally Server   │ (http://localhost:9000/)
│  Chennai Oxygen │
└────────┬────────┘
         │ XML API (10s cooldown)
         ↓
┌─────────────────┐
│  CO Middleware  │
│  ┌───────────┐  │
│  │ Dashboard │  │ (http://localhost:8787)
│  │  (Flask)  │  │
│  └───────────┘  │
│  ┌───────────┐  │
│  │Automation │  │ (Background threads)
│  │ Manager   │  │
│  └───────────┘  │
│  ┌───────────┐  │
│  │  SQLite   │  │ (new.sqlite)
│  │ Database  │  │
│  └───────────┘  │
└────────┬────────┘
         │ REST API
         ↓
┌─────────────────┐
│ Catalytics API  │ (http://192.168.1.43:8000/)
│    Backend      │
└─────────────────┘
```

---

## How It Works

### 1. Fetch Flow (Tally → SQLite)

```
┌──────────────────────────────────────────────────────────┐
│                    FETCH FLOW                            │
└──────────────────────────────────────────────────────────┘

Step 1: Get Companies
  ↓
  tally_client.get_companies(url)
  ↓
  Returns: ["Chennai Oxygen"]

Step 2: Fetch Customers
  ↓
  tally_client.get_ledgers(company, url)
  ↓
  XML: <COLLECTION NAME="Ledger"><TYPE>Ledger</TYPE></COLLECTION>
  ↓
  Gets: ALL ledgers (customers, suppliers, banks, etc.)
  ↓
  Filter in Python: parent in ["sundry debtors", "debtors", "receivables"]
  ↓
  Returns: [Kamal, Shri Amman Gas Agency, Thirumalai Agency]
  ↓
  Save to: ledgers table

Step 3: Fetch Products
  ↓
  tally_client.get_stock_items(company, url)
  ↓
  XML: <COLLECTION NAME="StockItem"><TYPE>StockItem</TYPE></COLLECTION>
  ↓
  Gets: ALL stock items (no filters)
  ↓
  Returns: [ARGON 7CM(CYL), CARBONDIOXIDE 7L(CYL), ...]
  ↓
  Save to: stock_items table

Step 4: Fetch DCs
  ↓
  tally_client.get_delivery_notes(company, url, from_date, to_date)
  ↓
  Method: Fetch ALL vouchers, filter for "delivery" keywords
  ↓
  Returns: [DC #1 (Date: 2025-04-01, Party: Shri Amman Gas Agency)]
  ↓
  Save to: delivery_notes table + delivery_note_items table
```

### 2. Sync Flow (SQLite → Catalytics)

```
┌──────────────────────────────────────────────────────────┐
│                    SYNC FLOW                             │
└──────────────────────────────────────────────────────────┘

Step 1: Get Unsynced Records
  ↓
  Query: SELECT * FROM ledgers WHERE is_synced = 0
  Query: SELECT * FROM stock_items WHERE is_synced = 0
  Query: SELECT * FROM delivery_notes WHERE is_synced = 0

Step 2: Transform Data
  ↓
  Convert Tally format → Catalytics format
  ↓
  Example:
    Tally: {"NAME": "Kamal", "PARENT": "Sundry Debtors"}
    ↓
    Catalytics: {"name": "Kamal", "entity_id": 1, "tally_guid": "..."}

Step 3: Send to API
  ↓
  POST http://192.168.1.43:8000/import/tally-customer-payload/
  POST http://192.168.1.43:8000/import/tally-product-payload/
  POST http://192.168.1.43:8000/import/tally-invoice-payload/
  ↓
  Headers: {"Authorization": "Bearer <API_KEY>"}

Step 4: Update Sync Status
  ↓
  If success: UPDATE sync_status SET is_synced = 1
  If error: UPDATE sync_status SET last_error = "...", attempts = attempts + 1
```

### 3. Automation Flow

```
┌──────────────────────────────────────────────────────────┐
│                 AUTOMATION THREADS                       │
└──────────────────────────────────────────────────────────┘

Thread 1: Fetch Master Data (Every 30 minutes)
  ↓
  Wait 20 seconds (initial delay)
  ↓
  Fetch Customers → Save to DB
  ↓
  Wait 20 seconds (between fetches)
  ↓
  Fetch Products → Save to DB
  ↓
  Sleep 30 minutes
  ↓
  Repeat

Thread 2: Fetch DCs (Every 5 minutes)
  ↓
  Wait 30 seconds (initial delay)
  ↓
  Fetch DCs → Save to DB
  ↓
  Sleep 5 minutes
  ↓
  Repeat

Thread 3: Sync to Catalytics (Every 5 seconds)
  ↓
  Wait 60 seconds (initial delay)
  ↓
  Sync Customers → Update sync_status
  ↓
  Sync Products → Update sync_status
  ↓
  Sync DCs → Update sync_status
  ↓
  Sleep 5 seconds
  ↓
  Repeat
```

---

## Setup & Configuration

### 1. Environment Variables (.env)

```bash
# Tally Configuration
TALLY_URL=http://192.168.1.44:9000/
TALLY_COMPANY=Chennai Oxygen
TALLY_DB_PATH=/path/to/CO_middleware/new.sqlite
TALLY_FETCH_STOCK=false  # Speed optimization

# Catalytics API Configuration
CATALYTICS_ENTITY_ID=1
CATALYTICS_API_BASE_URL=http://192.168.1.43:8000/import
CATALYTICS_API_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...

# Automation Settings
AUTO_START_AUTOMATION=true
UI_AUTO_START=true
SYNC_INTERVAL=5  # seconds

# Web UI
WEB_UI_HOST=0.0.0.0
WEB_UI_PORT=8787

# Logging
LOG_LEVEL=INFO
LOG_JSON=false
```

### 2. Database Schema

**companies** - Tally company information
```sql
CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    tally_name TEXT,
    entity_id INTEGER,
    tally_url TEXT,
    created_at TEXT,
    updated_at TEXT
);
```

**ledgers** - Customers (Sundry Debtors)
```sql
CREATE TABLE ledgers (
    id INTEGER PRIMARY KEY,
    company_id INTEGER,
    name TEXT NOT NULL,
    data_json TEXT,  -- Full Tally JSON
    created_at TEXT,
    updated_at TEXT,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY(company_id) REFERENCES companies(id)
);
```

**stock_items** - Products
```sql
CREATE TABLE stock_items (
    id INTEGER PRIMARY KEY,
    company_id INTEGER,
    name TEXT NOT NULL,
    data_json TEXT,  -- Full Tally JSON
    created_at TEXT,
    updated_at TEXT,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY(company_id) REFERENCES companies(id)
);
```

**delivery_notes** - DCs
```sql
CREATE TABLE delivery_notes (
    id INTEGER PRIMARY KEY,
    company_id INTEGER,
    dc_no TEXT NOT NULL,
    voucher_date TEXT,
    party_ledger_name TEXT,
    reference TEXT,
    data_json TEXT,  -- Full Tally JSON
    created_at TEXT,
    updated_at TEXT,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY(company_id) REFERENCES companies(id)
);
```

**sync_status** - Tracks sync status for each record
```sql
CREATE TABLE sync_status (
    id INTEGER PRIMARY KEY,
    delivery_note_id INTEGER,
    is_synced INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    last_attempt_at TEXT,
    synced_at TEXT,
    last_error TEXT,
    FOREIGN KEY(delivery_note_id) REFERENCES delivery_notes(id)
);
```

---

## Running the Middleware

### Start Dashboard

```bash
cd CO_middleware
python dashboard.py
```

Dashboard will be available at: `http://localhost:8787`

### Manual Commands

**Fetch Customers:**
```bash
python fetch_customers.py \
  --db-path new.sqlite \
  --tally-url http://192.168.1.44:9000/ \
  --company "Chennai Oxygen" \
  --entity-id 1
```

**Fetch Products:**
```bash
python fetch_products.py \
  --db-path new.sqlite \
  --tally-url http://192.168.1.44:9000/ \
  --company "Chennai Oxygen" \
  --entity-id 1
```

**Fetch DCs:**
```bash
python fetch_invoices.py \
  --db-path new.sqlite \
  --tally-url http://192.168.1.44:9000/ \
  --company "Chennai Oxygen" \
  --entity-id 1 \
  --from-date 20200101 \
  --to-date 20991231
```

**Sync to Catalytics:**
```bash
python sync_catalytics.py \
  --db-path new.sqlite \
  --api-base-url http://192.168.1.43:8000/import \
  --api-key "your-api-key" \
  --entity-id 1
```

---

## Code Flow Explained

### 1. Tally Client (tally_client.py)

**Purpose**: Communicate with Tally XML API

**Key Functions:**

```python
def send_request(xml_request, url, timeout=60):
    """
    Send XML request to Tally with 10-second cooldown
    
    Flow:
    1. Acquire global lock (serialize requests)
    2. Send POST request with XML
    3. Wait for response
    4. Sleep 10 seconds (cooldown)
    5. Release lock
    
    Why cooldown?
    - Tally crashes with "c0000005 Memory Access Violation" 
      if requests come too fast
    - 10 seconds gives Tally time to free memory
    """
    with _tally_lock:
        try:
            resp = requests.post(url, data=xml_request, ...)
            return resp.text
        finally:
            time.sleep(10.0)  # CRITICAL for stability
```

```python
def get_delivery_notes(company_name, url, from_date, to_date):
    """
    Fetch DCs from Tally
    
    Method: Fetch ALL vouchers, filter in Python
    
    Why not use FILTERS?
    - Tally Collection with FILTERS returns empty data
    - This is a known Tally API limitation
    
    Flow:
    1. Build XML to fetch ALL vouchers
    2. Send request (10s cooldown)
    3. Parse response
    4. Filter for voucher types containing:
       - "delivery"
       - "challan"
       - "dc"
       - "delv"
    5. Return filtered list
    """
    xml = f"""
    <COLLECTION ISMODIFY="No" NAME="AllVouchers">
      <TYPE>Voucher</TYPE>
      <FETCH>*</FETCH>
      <FETCH>INVENTORYENTRIES.STOCKITEMNAME</FETCH>
      ...
    </COLLECTION>
    """
    
    resp = send_request(xml, url)
    all_vouchers = parse_delivery_notes(resp)
    
    # Filter in Python
    delivery_keywords = ["delivery", "challan", "dc", "delv"]
    dc_vouchers = [v for v in all_vouchers 
                   if any(kw in v.get("VOUCHERTYPENAME", "").lower() 
                         for kw in delivery_keywords)]
    
    return dc_vouchers
```

### 2. Fetch Scripts (fetch_*.py)

**Purpose**: Fetch data from Tally and save to SQLite

**Common Pattern:**

```python
def run_once(config):
    """
    Standard fetch flow
    
    1. Connect to database
    2. Get company from Tally
    3. Ensure company exists in DB
    4. Fetch data from Tally
    5. For each record:
       - Check if exists in DB
       - Insert or update
       - Mark as unsynced if changed
    6. Detect deletions (records in DB but not in Tally)
    7. Commit transaction
    """
    
    conn = db.connect(config.db_path)
    
    # Get company
    companies = tally_api.get_companies(config.tally_url)
    company_id = db.ensure_company(conn, ...)
    
    # Fetch data
    records = tally_api.get_xxx(company_name, config.tally_url)
    
    # Process each record
    for record in records:
        # Check if exists
        existing = conn.execute(
            "SELECT data_json FROM table WHERE company_id = ? AND name = ?",
            (company_id, record["NAME"])
        ).fetchone()
        
        # Insert or update
        record_id = db.upsert_xxx(conn, company_id, record["NAME"], record)
        
        # Mark as unsynced if changed
        if is_changed(existing, record):
            db.ensure_sync_status(conn, record_id, is_synced=0)
    
    # Detect deletions
    tally_names = {r["NAME"] for r in records}
    db_records = conn.execute("SELECT name FROM table WHERE company_id = ?", (company_id,))
    
    for db_record in db_records:
        if db_record["name"] not in tally_names:
            db.mark_deleted(conn, db_record["name"])
    
    conn.commit()
```

### 3. Sync Scripts (sync_*.py)

**Purpose**: Sync data from SQLite to Catalytics API

**Flow:**

```python
def run_once(config):
    """
    Standard sync flow
    
    1. Connect to database
    2. Get unsynced records (is_synced = 0)
    3. For each record:
       - Transform to Catalytics format
       - Send to API
       - If success: mark as synced
       - If error: increment attempts, save error
    4. Commit transaction
    """
    
    conn = db.connect(config.db_path)
    
    # Get unsynced records
    unsynced = conn.execute("""
        SELECT r.*, ss.attempts 
        FROM records r
        LEFT JOIN sync_status ss ON ss.record_id = r.id
        WHERE COALESCE(ss.is_synced, 0) = 0
        AND COALESCE(ss.attempts, 0) < ?
        LIMIT ?
    """, (config.max_attempts, config.limit)).fetchall()
    
    # Sync each record
    for record in unsynced:
        try:
            # Transform data
            payload = transform_to_catalytics_format(record)
            
            # Send to API
            response = requests.post(
                f"{config.api_base_url}/tally-xxx-payload/",
                json=payload,
                headers={"Authorization": f"Bearer {config.api_key}"}
            )
            response.raise_for_status()
            
            # Mark as synced
            db.update_sync_status(
                conn, 
                record_id=record["id"],
                is_synced=1,
                synced_at=datetime.now()
            )
            
        except Exception as e:
            # Save error
            db.update_sync_status(
                conn,
                record_id=record["id"],
                is_synced=0,
                attempts=record["attempts"] + 1,
                last_error=str(e)
            )
    
    conn.commit()
```

### 4. Automation Manager (automation_manager.py)

**Purpose**: Run fetch and sync automatically in background threads

**Architecture:**

```python
class AutomationManager:
    """
    Manages 3 background threads:
    1. Fetch Master Data (customers + products)
    2. Fetch DCs
    3. Sync to Catalytics
    """
    
    def start(self):
        """Start all automation threads"""
        self._start_thread('fetch_master', self._fetch_master_loop)
        self._start_thread('fetch_invoices', self._fetch_invoices_loop)
        self._start_thread('sync', self._sync_loop)
    
    def _fetch_master_loop(self):
        """
        Fetch customers and products every 30 minutes
        
        Flow:
        1. Wait 20 seconds (initial delay)
        2. Fetch customers
        3. Wait 20 seconds (between fetches)
        4. Fetch products
        5. Sleep 30 minutes
        6. Repeat
        """
        time.sleep(20)  # Initial delay
        
        while not self.stop_flags['fetch_master'].is_set():
            # Fetch customers
            fetch_customers_once(config)
            
            time.sleep(20)  # Between fetches
            
            # Fetch products
            fetch_products_once(config)
            
            time.sleep(1800)  # 30 minutes
    
    def _fetch_invoices_loop(self):
        """
        Fetch DCs every 5 minutes
        
        Flow:
        1. Wait 30 seconds (initial delay)
        2. Fetch DCs
        3. Sleep 5 minutes
        4. Repeat
        """
        time.sleep(30)  # Initial delay
        
        while not self.stop_flags['fetch_invoices'].is_set():
            fetch_invoices_once(config)
            time.sleep(300)  # 5 minutes
    
    def _sync_loop(self):
        """
        Sync to Catalytics every 5 seconds
        
        Flow:
        1. Wait 60 seconds (initial delay)
        2. Sync DCs
        3. Sync customers
        4. Sync products
        5. Sleep 5 seconds
        6. Repeat
        """
        time.sleep(60)  # Initial delay
        
        while not self.stop_flags['sync'].is_set():
            sync_catalytics_once(config)  # DCs
            sync_customers_once(config)
            sync_products_once(config)
            time.sleep(5)  # 5 seconds
```

### 5. Dashboard (dashboard.py)

**Purpose**: Web UI for monitoring and manual control

**Key Endpoints:**

```python
@app.route('/api/status')
def api_status():
    """
    Get system status
    
    Returns:
    - Customer count (total, synced, unsynced)
    - Product count (total, synced, unsynced)
    - DC count (total, synced, unsynced)
    - Last sync times
    """
    conn = db.connect(db_path)
    
    customer_total = conn.execute('SELECT COUNT(*) FROM ledgers').fetchone()[0]
    customer_synced = conn.execute(
        'SELECT COUNT(*) FROM ledgers l JOIN sync_status s ON s.ledger_id = l.id WHERE s.is_synced = 1'
    ).fetchone()[0]
    
    return jsonify({
        'customers': {
            'total': customer_total,
            'synced': customer_synced,
            'unsynced': customer_total - customer_synced
        },
        ...
    })

@app.route('/api/trigger/fetch_customers', methods=['POST'])
def trigger_fetch_customers():
    """
    Manually trigger customer fetch
    
    Flow:
    1. Build config from .env
    2. Run fetch_customers_once()
    3. Return stats
    """
    from fetch_customers import build_config, run_once
    
    config = build_config(args)
    stats = run_once(config)
    
    return jsonify({'success': True, 'stats': stats})

@app.route('/api/automation/start', methods=['POST'])
def automation_start():
    """
    Start automation threads
    
    Flow:
    1. Get automation manager
    2. Call manager.start()
    3. Return status
    """
    manager = get_manager()
    success = manager.start()
    
    return jsonify({'success': success})
```

---

## Troubleshooting

### Issue: Tally Crashes with "c0000005 Memory Access Violation"

**Cause**: Too many requests too fast

**Solution**: 
- 10-second cooldown is already implemented
- Increase intervals in .env if still crashing:
  ```bash
  SYNC_INTERVAL=10  # Increase from 5 to 10
  ```

### Issue: DCs Not Fetching

**Cause**: Collection with FILTERS doesn't work in your Tally version

**Solution**: Already fixed - now fetches ALL vouchers and filters in Python

### Issue: Fetch Takes Too Long

**Cause**: `TALLY_FETCH_STOCK=true` causes extra fetches

**Solution**: Set to `false` in .env:
```bash
TALLY_FETCH_STOCK=false
```

This skips fetching customer/product details during DC fetch (they're already in DB)

### Issue: Database Locked Error

**Cause**: Multiple processes accessing database

**Solution**:
```bash
# Stop dashboard
pkill -f dashboard.py

# Checkpoint WAL
sqlite3 new.sqlite "PRAGMA wal_checkpoint(TRUNCATE); VACUUM;"

# Restart dashboard
python dashboard.py
```

### Issue: Customers Not Showing in UI

**Cause**: Not fetched yet

**Solution**: Click "Fetch Master Data" button in dashboard

### Issue: Sync Failing

**Cause**: Wrong API key or URL

**Solution**: Check .env:
```bash
CATALYTICS_API_BASE_URL=http://192.168.1.43:8000/import
CATALYTICS_API_KEY=<valid-jwt-token>
```

---

## Testing

### Test Files

**test_current_tally.py** - Check what's in Tally
```bash
python test_current_tally.py
```

**test_complete_flow.py** - Test entire flow
```bash
python test_complete_flow.py
```

**test_dc_simple.py** - Quick DC fetch test
```bash
python test_dc_simple.py
```

**verify_after_restart.py** - Verify after restart
```bash
python verify_after_restart.py
```

### Quick Database Check

```bash
./show_database.sh
```

Or manually:
```bash
sqlite3 new.sqlite "SELECT COUNT(*) FROM ledgers;"
sqlite3 new.sqlite "SELECT COUNT(*) FROM stock_items;"
sqlite3 new.sqlite "SELECT COUNT(*) FROM delivery_notes;"
```

---

## Performance Optimization

### Current Settings (Optimized for 8GB RAM)

**Cooldown**: 10 seconds between ALL Tally requests
- Prevents crashes
- Serializes requests (no concurrent access)

**Fetch Intervals**:
- Master data: 30 minutes (customers + products)
- DCs: 5 minutes
- Sync: 5 seconds

**Speed Optimization**:
- `TALLY_FETCH_STOCK=false` - Skip extra fetches during DC fetch
- Reduces DC fetch time from 30s to 22s (25% faster)

### If You Need Faster Fetching

**Option 1**: Reduce cooldown (risky on 8GB RAM)
```python
# In tally_client.py
_TALLY_REQUEST_COOLDOWN = 5.0  # Reduce from 10 to 5
```

**Option 2**: Increase RAM on Tally server
- 16GB+ RAM can handle faster requests
- Can reduce cooldown to 2-5 seconds

**Option 3**: Use Tally's ODBC interface (future enhancement)
- Faster than XML API
- Requires Tally ODBC license

---

## Summary

**Data Flow:**
```
Tally → fetch_*.py → SQLite → sync_*.py → Catalytics API
```

**Automation:**
- 3 background threads running continuously
- Configurable intervals
- Automatic retry on errors

**Safety:**
- 10-second cooldown prevents Tally crashes
- Global lock serializes requests
- WAL mode for database concurrency

**Monitoring:**
- Web dashboard at http://localhost:8787
- Real-time status updates
- Manual trigger buttons
- Error logs and sync status

**Current Status:**
- ✅ 3 Customers
- ✅ 6 Products
- ✅ 1 DC
- ✅ All syncing to Catalytics

---

## Quick Start

```bash
# 1. Start dashboard
cd CO_middleware
python dashboard.py

# 2. Open browser
http://localhost:8787

# 3. Click buttons to fetch data
- "Fetch Master Data" → Customers + Products
- "Fetch Invoices" → DCs
- "Sync to Catalytics" → Send to backend

# 4. Monitor automation
- Check "Terminal Output" section
- See real-time logs
- Verify sync status in tables
```

That's it! The middleware is now running and will automatically fetch and sync data.
