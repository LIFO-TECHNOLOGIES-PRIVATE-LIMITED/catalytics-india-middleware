# Arasan Gas Middleware

Multi-company Tally integration middleware with duplicate prevention and data verification.

## Features

✅ **Multi-Company Support** - Fetch from up to 4 Tally companies
✅ **Duplicate Prevention** - First-come-first-served for customers/products
✅ **Company Tracking** - Know which company owns each record
✅ **Data Verification** - Verify sync in PostgreSQL before marking complete
✅ **Comprehensive Logging** - Track all operations with detailed logs
✅ **Flexible Configuration** - Enable/disable companies via .env

---

## Quick Start

### 1. Setup Configuration

```bash
# Copy example config
cp .env.example .env

# Edit .env with your settings
nano .env
```

**Key settings:**
```ini
# Entity Configuration
ENTITY_NAME=arasan_gas
ENTITY_ID=25

# Catalytics Backend
CATALYTICS_API_BASE=http://localhost:8000
CATALYTICS_API_KEY=your-api-key-here

# Tally Companies
TALLY_COMPANY_1=BHARATH OXYGEN LICENSEE
TALLY_COMPANY_2=COMPANY NAME 2
TALLY_COMPANY_ACTIVE=COMPANY_1,COMPANY_2
```

### 2. Initialize Database

```bash
python db.py
```

This creates `arasan_gas.sqlite` with all required tables.

### 3. Test Tally Connection

```bash
python tally_client.py
```

---

## Usage

### Fetch Data from Tally

**Fetch Customers (with duplicate prevention):**
```bash
python fetch_customers.py
```

Expected output:
```
[2026-02-24 18:30:15] [INFO] Processing: BHARATH OXYGEN LICENSEE (COMPANY_1)
[2026-02-24 18:30:16] [INFO] Fetched 120 customers from Tally
[2026-02-24 18:30:17] [INFO] [NEW CUSTOMER] 'ABC LTD' saved (company: COMPANY_1)
[2026-02-24 18:30:45] [INFO] Processing: COMPANY 2 (COMPANY_2)
[2026-02-24 18:30:46] [WARNING] [DUPLICATE SKIPPED] 'ABC LTD' (owned by COMPANY_1)
[2026-02-24 18:30:47] [INFO] [NEW CUSTOMER] 'XYZ Corp' saved (company: COMPANY_2)
```

**Fetch Products (with duplicate prevention):**
```bash
python fetch_products.py
```

**Fetch Invoices (multi-company):**
```bash
# Today's invoices
python fetch_invoices.py

# Specific date range (YYYYMMDD format)
python fetch_invoices.py 20260224 20260224
```

### Sync to Catalytics

**Sync All Data with Verification:**
```bash
python sync_to_catalytics.py
```

This will:
1. Sync customers to Catalytics
2. Verify each customer in PostgreSQL
3. Sync products to Catalytics
4. Verify each product in PostgreSQL
5. Sync invoices as delivery challans
6. Verify each DC in PostgreSQL

Expected output:
```
[2026-02-24 18:31:10] [INFO] Syncing customer 'ABC LTD'...
[2026-02-24 18:31:11] [INFO] API sync successful: Customer ID=1001
[2026-02-24 18:31:11] [INFO] ✓ VERIFIED: Customer 'ABC LTD' exists (ID=1001)
[2026-02-24 18:31:11] [INFO] ✓ SUCCESS: Customer 'ABC LTD' synced and verified
```

---

## Complete Workflow

### Daily Operations

```bash
# 1. Fetch latest data (run every 10 minutes)
python fetch_customers.py
python fetch_products.py

# 2. Fetch invoices (run every 30 seconds)
python fetch_invoices.py

# 3. Sync to Catalytics (run every 30 seconds)
python sync_to_catalytics.py
```

### Scheduled Execution

**Windows Task Scheduler:**
- Customer/Product fetch: Every 10 minutes
- Invoice fetch: Every 30 seconds
- Sync to Catalytics: Every 30 seconds

**Linux Cron:**
```cron
# Fetch customers/products every 10 minutes
*/10 * * * * cd /path/to/arasan_gas && python fetch_customers.py
*/10 * * * * cd /path/to/arasan_gas && python fetch_products.py

# Fetch invoices every 30 seconds
* * * * * cd /path/to/arasan_gas && python fetch_invoices.py
* * * * * sleep 30 && cd /path/to/arasan_gas && python fetch_invoices.py

# Sync to Catalytics every 30 seconds
* * * * * cd /path/to/arasan_gas && python sync_to_catalytics.py
* * * * * sleep 30 && cd /path/to/arasan_gas && python sync_to_catalytics.py
```

---

## How Duplicate Prevention Works

### Scenario: 4 Companies with Same Customer

```
Company 1 fetches "ARS STEEL" → Saved (owned by Company 1) ✓
Company 2 fetches "ARS STEEL" → Skipped (duplicate) ✗
Company 3 fetches "ARS STEEL" → Skipped (duplicate) ✗
Company 4 fetches "ARS STEEL" → Skipped (duplicate) ✗

Result: Only 1 "ARS STEEL" in database
```

### Database Tracking

```sql
-- Customers table
id  | name       | tally_company | is_synced | catalytics_id
1   | ARS STEEL  | COMPANY_1     | 1         | 1001
2   | XYZ Corp   | COMPANY_2     | 1         | 1002
```

### Duplicate Log

All duplicate attempts are logged for audit:

```sql
-- duplicate_log table
entity_type | entity_name | tally_company | owned_by_company
customer    | ARS STEEL   | COMPANY_2     | COMPANY_1
customer    | ARS STEEL   | COMPANY_3     | COMPANY_1
```

---

## Verification Process

### How Verification Works

```
1. Sync customer to Catalytics API
   ↓
   Response: {"customer_id": 1001}

2. Verify in PostgreSQL
   ↓
   GET /api/customer/1001/
   ↓
   Found: {"id": 1001, "name": "ABC LTD"} ✓

3. Mark as synced in SQLite
   ↓
   is_synced = 1, catalytics_id = 1001
```

### If Verification Fails

```
1. API returns success
2. But NOT found in PostgreSQL ✗
3. Keep as unsynced (is_synced = 0)
4. Will retry next sync cycle
```

---

## Database Statistics

```bash
python -c "
from db import Database
db = Database('arasan_gas.sqlite')
stats = db.get_statistics()
for key, value in stats.items():
    print(f'{key}: {value}')
"
```

Output:
```
total_customers: 157
synced_customers: 145
customers_by_company: {'COMPANY_1': 120, 'COMPANY_2': 37}
total_products: 89
synced_products: 89
products_by_company: {'COMPANY_1': 65, 'COMPANY_2': 24}
total_duplicates: 45
duplicates_by_type: {'customer': 23, 'product': 22}
```

---

## Configuration Options

### Active Company Control

Enable/disable companies without code changes:

```ini
# Enable Company 1 and 2
TALLY_COMPANY_ACTIVE=COMPANY_1,COMPANY_2

# Enable all 4 companies
TALLY_COMPANY_ACTIVE=COMPANY_1,COMPANY_2,COMPANY_3,COMPANY_4

# Enable only Company 1
TALLY_COMPANY_ACTIVE=COMPANY_1
```

### Verification Control

```ini
# Enable verification (recommended)
VERIFY_AFTER_SYNC=true

# Disable verification (faster but risky)
VERIFY_AFTER_SYNC=false

# Verification retry settings
VERIFY_RETRY_COUNT=3
VERIFY_RETRY_DELAY_SECONDS=1
```

---

## Logs

All operations are logged to `logs/arasan_gas.log`:

```bash
# View live logs
tail -f logs/arasan_gas.log

# View last 100 lines
tail -100 logs/arasan_gas.log

# Search for errors
grep "ERROR" logs/arasan_gas.log

# Search for duplicates
grep "DUPLICATE SKIPPED" logs/arasan_gas.log
```

---

## Troubleshooting

### Issue: No data fetched from Tally

**Check:**
```bash
# Test Tally connection
python tally_client.py
```

**Solution:**
- Verify Tally is running on port 9000
- Check company name is correct in .env
- Ensure Tally ODBC is enabled

### Issue: Sync successful but no data in Catalytics

**Check:**
```bash
# Check if backend is running
curl http://localhost:8000/api/health/
```

**Solution:**
- Start Django backend
- Check API key is correct
- Verify entity ID matches database

### Issue: Duplicates not being skipped

**Check:**
```bash
# Verify unique constraints
python -c "
from db import Database
db = Database('arasan_gas.sqlite')
# Try inserting duplicate
db.insert_customer({
    'tally_guid': 'TEST',
    'name': 'ABC LTD',
    'tally_company': 'COMPANY_2'
})
"
```

Expected: Should raise unique constraint error

---

## File Structure

```
arasan_gas/
├── .env                         # Configuration (create from .env.example)
├── .env.example                 # Configuration template
├── README.md                    # This file
├── config.py                    # Configuration loader
├── db.py                        # Database schema and operations
├── tally_client.py              # Tally XML API client
├── fetch_customers.py           # Fetch customers with duplicate prevention
├── fetch_products.py            # Fetch products with duplicate prevention
├── fetch_invoices.py            # Fetch invoices (multi-company)
├── sync_to_catalytics.py        # Sync with verification
├── verify_sync.py               # Verification utility
├── arasan_gas.sqlite            # SQLite database (created on first run)
├── logs/
│   └── arasan_gas.log           # Application logs
└── DATA_VERIFICATION_LOGIC.md   # Detailed verification documentation
```

---

## Support

For issues:
1. Check logs: `tail -f logs/arasan_gas.log`
2. Verify configuration: `python config.py`
3. Test Tally connection: `python tally_client.py`
4. Check database: Query arasan_gas.sqlite

---

## Version

- **Version:** 1.0.0
- **Date:** 2026-02-24
- **Status:** Production Ready ✓
