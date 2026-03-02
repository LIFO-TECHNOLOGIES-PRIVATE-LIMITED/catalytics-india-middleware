# Arasan Gas Middleware - Implementation Plan

## Overview

Create a separate middleware instance for **Arasan Gas** entity that handles multiple Tally companies (4 companies) with duplicate prevention for customers and products.

---

## Problem Statement

**Scenario:**
- Client has 4 Tally companies for accounting purposes
- All 4 companies share the SAME stock (products) and customers
- Invoices can be created in ANY of the 4 companies
- When fetching data, duplicate customer/product names cause conflicts

**Challenge:**
- Company 1 fetches "ARS STEEL" customer → Saved to Catalytics
- Company 2 also has "ARS STEEL" customer → Should be REJECTED (duplicate)
- Same logic applies to products

**Solution:**
- **First-Come-First-Served**: Whichever company fetches first "owns" the record
- Subsequent duplicates are skipped with proper logging
- Maintain data consistency between SQLite (local) and PostgreSQL (backend)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    TALLY SERVER (Local)                      │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐           │
│  │ Company 1   │ │ Company 2   │ │ Company 3,4 │           │
│  │ (Active)    │ │ (Active)    │ │ (Inactive)  │           │
│  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘           │
└─────────┼────────────────┼────────────────┼─────────────────┘
          │                │                │
          └────────────────┴────────────────┘
                           │
                    XML API (Port 9000)
                           │
                           ↓
┌─────────────────────────────────────────────────────────────┐
│           ARASAN GAS MIDDLEWARE (arasan_gas/)                │
│                                                               │
│  .env Configuration:                                          │
│  - ENTITY_NAME=arasan_gas                                     │
│  - TALLY_COMPANY_1=COMPANY_NAME_1                            │
│  - TALLY_COMPANY_2=COMPANY_NAME_2                            │
│  - TALLY_COMPANY_ACTIVE=COMPANY_1,COMPANY_2                  │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐   │
│  │         SQLite Local Database                         │   │
│  │  - customers (with tally_company field)               │   │
│  │  - products (with tally_company field)                │   │
│  │  - invoices (with tally_company field)                │   │
│  │  - sync_status (track sync state)                     │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                               │
│  Duplicate Prevention Logic:                                 │
│  1. Fetch from Company 1 → Check name uniqueness             │
│  2. If name exists in SQLite → Skip with log                 │
│  3. If new → Save to SQLite with company source              │
│  4. Repeat for Company 2, 3, 4                               │
│                                                               │
└───────────────────────────┬───────────────────────────────────┘
                            │
                    HTTP API (Port 8000)
                            │
                            ↓
┌─────────────────────────────────────────────────────────────┐
│        CATALYTICS BACKEND (arasan_gas database)              │
│                                                               │
│  PostgreSQL Database:                                         │
│  - master.Customer (unique constraint on name)               │
│  - master.Product (unique constraint on name)                │
│  - transaction.DeliveryChallan                               │
│  - transaction.OrderDetails                                  │
│                                                               │
│  Sync Verification:                                           │
│  - Check SQLite record exists in PostgreSQL                  │
│  - Update sync_status only if verified                       │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

---

## Implementation Steps

### Phase 1: Setup Project Structure

**Step 1.1: Create arasan_gas folder**
```bash
mkdir C:/Github/catalytics-india-middleware/arasan_gas
cd C:/Github/catalytics-india-middleware/arasan_gas
```

**Step 1.2: Copy base middleware structure**
```
arasan_gas/
├── .env                    # Configuration
├── .env.example
├── config.py               # Multi-company config loader
├── db.py                   # SQLite database schema
├── tally_client.py         # Tally XML API client
├── fetch_customers.py      # Customer fetch with duplicate check
├── fetch_products.py       # Product fetch with duplicate check
├── fetch_invoices.py       # Invoice fetch (DC creation)
├── sync_customers.py       # Sync customers to backend
├── sync_products.py        # Sync products to backend
├── sync_invoices_to_dc.py  # Sync invoices as DCs
├── web_ui.py               # Control panel UI
├── logs/
└── arasan_gas.sqlite       # Local SQLite database
```

---

### Phase 2: Configuration (.env)

**File: arasan_gas/.env**
```ini
# Entity Configuration
ENTITY_NAME=arasan_gas
ENTITY_ID=25

# Catalytics Backend API
CATALYTICS_API_BASE=http://localhost:8000
CATALYTICS_API_KEY=your-api-key-here

# Tally Configuration
TALLY_URL=http://localhost:9000/

# Multi-Company Configuration
TALLY_COMPANY_1=BHARATH OXYGEN LICENSEE
TALLY_COMPANY_2=ARASAN GAS COMPANY 2
TALLY_COMPANY_3=ARASAN GAS COMPANY 3
TALLY_COMPANY_4=ARASAN GAS COMPANY 4

# Active Companies (comma-separated)
TALLY_COMPANY_ACTIVE=COMPANY_1,COMPANY_2

# Sync Configuration
SYNC_INTERVAL_SECONDS=30
FETCH_CUSTOMERS_INTERVAL_MINUTES=10
FETCH_PRODUCTS_INTERVAL_MINUTES=10

# Database
SQLITE_DB_PATH=arasan_gas.sqlite
```

---

### Phase 3: SQLite Schema with Company Tracking

**File: arasan_gas/db.py**

**Key Changes:**
1. Add `tally_company` field to track which company owns the record
2. Add `first_fetched_at` timestamp
3. Add unique constraints on name fields
4. Add sync_status tracking

**Tables:**
```sql
CREATE TABLE customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tally_guid TEXT UNIQUE NOT NULL,
    name TEXT UNIQUE NOT NULL,           -- Unique constraint for duplicate prevention
    tally_company TEXT NOT NULL,         -- Which company owns this record
    gstin TEXT,
    pan TEXT,
    address TEXT,
    state TEXT,
    first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_synced INTEGER DEFAULT 0,
    catalytics_id INTEGER,
    sync_attempts INTEGER DEFAULT 0,
    last_sync_error TEXT
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tally_guid TEXT UNIQUE NOT NULL,
    name TEXT UNIQUE NOT NULL,           -- Unique constraint
    tally_company TEXT NOT NULL,         -- Which company owns this record
    hsn_code TEXT,
    unit TEXT,
    rate REAL,
    first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_synced INTEGER DEFAULT 0,
    catalytics_id INTEGER,
    sync_attempts INTEGER DEFAULT 0,
    last_sync_error TEXT
);

CREATE TABLE invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tally_voucher_no TEXT NOT NULL,
    tally_company TEXT NOT NULL,         -- Which company created this invoice
    voucher_date TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    total_amount REAL,
    first_fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_synced INTEGER DEFAULT 0,
    dc_no TEXT,                          -- Catalytics DC number after sync
    catalytics_dc_id INTEGER,
    sync_attempts INTEGER DEFAULT 0,
    last_sync_error TEXT,
    UNIQUE(tally_voucher_no, tally_company)  -- Unique per company
);

CREATE TABLE sync_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,             -- 'fetch_customers', 'sync_customers', etc.
    tally_company TEXT,                  -- Which company (if applicable)
    status TEXT NOT NULL,                -- 'running', 'success', 'failed'
    records_processed INTEGER DEFAULT 0,
    records_success INTEGER DEFAULT 0,
    records_failed INTEGER DEFAULT 0,
    records_skipped INTEGER DEFAULT 0,   -- Duplicates skipped
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    error_message TEXT
);
```

---

### Phase 4: Multi-Company Fetch Logic

**File: arasan_gas/fetch_customers.py**

**Duplicate Prevention Logic:**

```python
def fetch_customers_from_all_companies():
    """
    Fetch customers from all active Tally companies with duplicate prevention
    """
    active_companies = get_active_companies()  # From config

    stats = {
        'total_fetched': 0,
        'new_saved': 0,
        'duplicates_skipped': 0,
        'errors': 0
    }

    for company_name in active_companies:
        logger.info(f"Fetching customers from {company_name}...")

        # Step 1: Fetch customers from Tally
        customers = tally_client.get_customers(company_name)
        stats['total_fetched'] += len(customers)

        for customer in customers:
            customer_name = customer['name']

            # Step 2: Check if customer name already exists in SQLite
            existing = db.query(
                "SELECT id, tally_company FROM customers WHERE name = ?",
                (customer_name,)
            )

            if existing:
                # Duplicate found - skip and log
                logger.warning(
                    f"DUPLICATE SKIPPED: Customer '{customer_name}' already exists "
                    f"(owned by {existing['tally_company']}). "
                    f"Current company: {company_name}"
                )
                stats['duplicates_skipped'] += 1
                continue

            # Step 3: New customer - save to SQLite
            try:
                db.execute("""
                    INSERT INTO customers (
                        tally_guid, name, tally_company, gstin, pan,
                        address, state, first_fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """, (
                    customer['guid'],
                    customer_name,
                    company_name,  # Mark which company owns this
                    customer.get('gstin'),
                    customer.get('pan'),
                    customer.get('address'),
                    customer.get('state')
                ))

                logger.info(
                    f"NEW CUSTOMER: '{customer_name}' saved "
                    f"(company: {company_name})"
                )
                stats['new_saved'] += 1

            except Exception as e:
                logger.error(f"Error saving customer '{customer_name}': {e}")
                stats['errors'] += 1

    # Step 4: Log summary
    logger.info(f"""
    ========================================
    CUSTOMER FETCH SUMMARY
    ========================================
    Total Fetched: {stats['total_fetched']}
    New Saved: {stats['new_saved']}
    Duplicates Skipped: {stats['duplicates_skipped']}
    Errors: {stats['errors']}
    ========================================
    """)

    return stats
```

**Key Points:**
- ✅ Fetch from Company 1 first
- ✅ Check name uniqueness in SQLite
- ✅ If exists → Skip with warning log
- ✅ If new → Save with company ownership
- ✅ Repeat for Company 2, 3, 4
- ✅ Log summary showing duplicates skipped

---

### Phase 5: Product Fetch (Same Logic)

**File: arasan_gas/fetch_products.py**

Same duplicate prevention logic as customers:
- Check product name uniqueness
- First company to fetch "owns" the product
- Skip duplicates from other companies
- Log ownership and duplicates

---

### Phase 6: Invoice Fetch (Multi-Company Support)

**File: arasan_gas/fetch_invoices.py**

**Different Logic:**
- Invoices are NOT deduplicated by name
- Each company can have Invoice #1, Invoice #2, etc.
- Store with `tally_company` field to identify source
- Unique constraint: `(voucher_no, tally_company)`

```python
def fetch_invoices_from_all_companies(from_date, to_date):
    """
    Fetch invoices from all active companies
    Invoices are NOT deduplicated - each company can have same invoice numbers
    """
    active_companies = get_active_companies()

    for company_name in active_companies:
        invoices = tally_client.get_sales_invoices(
            company_name,
            from_date,
            to_date
        )

        for invoice in invoices:
            # Check if this specific invoice from this company already exists
            existing = db.query("""
                SELECT id FROM invoices
                WHERE tally_voucher_no = ? AND tally_company = ?
            """, (invoice['voucher_no'], company_name))

            if existing:
                # Already fetched this invoice from this company
                continue

            # Save new invoice
            db.execute("""
                INSERT INTO invoices (
                    tally_voucher_no, tally_company, voucher_date,
                    customer_name, total_amount
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                invoice['voucher_no'],
                company_name,  # Mark source company
                invoice['date'],
                invoice['customer_name'],
                invoice['total_amount']
            ))
```

---

### Phase 7: Sync to Catalytics Backend

**File: arasan_gas/sync_customers.py**

**Sync with Verification:**

```python
def sync_customers_to_catalytics():
    """
    Sync customers from SQLite to Catalytics backend
    Only sync if not already synced
    """
    # Get unsynced customers
    customers = db.query("""
        SELECT * FROM customers
        WHERE is_synced = 0
        ORDER BY first_fetched_at
    """)

    for customer in customers:
        try:
            # Call Catalytics API
            response = api.post('/api/master/customer/sync/', {
                'name': customer['name'],
                'gstin': customer['gstin'],
                'pan': customer['pan'],
                'address': customer['address'],
                'state': customer['state'],
                'entity_id': config.ENTITY_ID
            })

            if response.status_code == 200:
                catalytics_id = response.json()['customer_id']

                # Verify customer exists in Catalytics database
                verify_response = api.get(
                    f'/api/master/customer/{catalytics_id}/'
                )

                if verify_response.status_code == 200:
                    # Verified - mark as synced
                    db.execute("""
                        UPDATE customers
                        SET is_synced = 1,
                            catalytics_id = ?,
                            last_updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (catalytics_id, customer['id']))

                    logger.info(
                        f"SYNCED: Customer '{customer['name']}' "
                        f"(company: {customer['tally_company']}, "
                        f"Catalytics ID: {catalytics_id})"
                    )
                else:
                    logger.error(
                        f"VERIFICATION FAILED: Customer '{customer['name']}' "
                        f"not found in Catalytics after sync"
                    )

        except Exception as e:
            logger.error(f"Sync error for '{customer['name']}': {e}")
            db.execute("""
                UPDATE customers
                SET sync_attempts = sync_attempts + 1,
                    last_sync_error = ?
                WHERE id = ?
            """, (str(e), customer['id']))
```

---

### Phase 8: Invoice to DC Sync

**File: arasan_gas/sync_invoices_to_dc.py**

Normal DC sync logic:
- Fetch unsynced invoices from SQLite
- Map customer name to Catalytics customer ID
- Create DC in Catalytics
- Verify DC created
- Mark as synced

**Important:** Include `tally_company` in logs for debugging

---

### Phase 9: Web UI with Multi-Company Status

**File: arasan_gas/web_ui.py**

**Dashboard Enhancements:**

```html
<!-- Company Status Section -->
<div class="company-status">
    <h3>Active Tally Companies</h3>
    <table>
        <tr>
            <th>Company</th>
            <th>Status</th>
            <th>Customers Owned</th>
            <th>Products Owned</th>
            <th>Invoices</th>
        </tr>
        <tr>
            <td>Company 1</td>
            <td>Active</td>
            <td>45</td>
            <td>120</td>
            <td>234</td>
        </tr>
        <tr>
            <td>Company 2</td>
            <td>Active</td>
            <td>12</td>
            <td>30</td>
            <td>89</td>
        </tr>
    </table>
</div>

<!-- Duplicate Statistics -->
<div class="duplicate-stats">
    <h3>Duplicate Prevention</h3>
    <p>Customers Skipped (Duplicates): 23</p>
    <p>Products Skipped (Duplicates): 15</p>
</div>
```

---

## Data Flow Example

### Scenario: 4 Companies with Duplicate Customer "ARS STEEL"

**Step 1: Fetch Customers**
```
Company 1 fetch → "ARS STEEL" found → Save to SQLite (owned by Company 1)
Company 2 fetch → "ARS STEEL" found → Check SQLite → Already exists → SKIP
Company 3 fetch → "ARS STEEL" found → Check SQLite → Already exists → SKIP
Company 4 fetch → "ARS STEEL" found → Check SQLite → Already exists → SKIP

Result: Only 1 "ARS STEEL" in SQLite (owned by Company 1)
```

**Step 2: Sync to Catalytics**
```
Sync "ARS STEEL" → Catalytics API → Saved with ID=1001
Verify in PostgreSQL → EXISTS → Mark synced in SQLite
```

**Step 3: Invoice Creation**
```
Company 2 creates Invoice #101 for "ARS STEEL"
Fetch invoice → Customer "ARS STEEL" already in Catalytics (ID=1001)
Create DC in Catalytics with customer_id=1001
```

---

## Testing Strategy

### Test Case 1: Duplicate Customer Prevention
```
Given: Company 1 has customer "ABC LTD"
And: Company 2 also has customer "ABC LTD"
When: Fetch customers from both companies
Then: Only 1 "ABC LTD" saved in SQLite
And: "ABC LTD" owned by Company 1
And: Company 2 duplicate logged as skipped
```

### Test Case 2: Multi-Company Invoice Sync
```
Given: Customer "ABC LTD" owned by Company 1
When: Company 2 creates invoice for "ABC LTD"
Then: Invoice fetched with company_name=Company 2
And: Mapped to existing customer ID in Catalytics
And: DC created successfully
```

### Test Case 3: Data Consistency Verification
```
Given: 50 customers in SQLite (is_synced=1)
When: Run verification script
Then: All 50 customers exist in Catalytics PostgreSQL
And: All IDs match
And: No sync status discrepancies
```

---

## Configuration Management

### Active Company Selection

**.env File:**
```ini
# Enable/disable companies without code changes
TALLY_COMPANY_ACTIVE=COMPANY_1,COMPANY_2

# To disable Company 2, just remove from list:
TALLY_COMPANY_ACTIVE=COMPANY_1
```

**Code (config.py):**
```python
def get_active_companies():
    """Get list of active company names to fetch from"""
    active = os.getenv('TALLY_COMPANY_ACTIVE', '').split(',')
    company_map = {
        'COMPANY_1': os.getenv('TALLY_COMPANY_1'),
        'COMPANY_2': os.getenv('TALLY_COMPANY_2'),
        'COMPANY_3': os.getenv('TALLY_COMPANY_3'),
        'COMPANY_4': os.getenv('TALLY_COMPANY_4'),
    }

    return [company_map[key] for key in active if key in company_map]
```

---

## Logging Strategy

### Log Format
```
[2026-02-24 18:30:15] [Company 1] [FETCH] Found 120 customers
[2026-02-24 18:30:16] [Company 1] [NEW] Customer "ABC LTD" saved
[2026-02-24 18:30:45] [Company 2] [FETCH] Found 125 customers
[2026-02-24 18:30:46] [Company 2] [DUPLICATE] Customer "ABC LTD" skipped (owned by Company 1)
[2026-02-24 18:30:47] [Company 2] [NEW] Customer "XYZ Corp" saved
[2026-02-24 18:31:10] [SYNC] Customer "ABC LTD" synced to Catalytics (ID=1001)
[2026-02-24 18:31:30] [Company 2] [INVOICE] Invoice #101 for "ABC LTD" → DC created (DC #5001)
```

---

## Error Handling

### Common Scenarios

**1. Tally Company Not Found**
```python
try:
    customers = tally_client.get_customers(company_name)
except TallyCompanyNotFoundError:
    logger.error(f"Company '{company_name}' not found in Tally")
    # Mark company as inactive in config
```

**2. Sync Verification Failed**
```python
if not verify_in_catalytics(customer_id):
    # Don't mark as synced
    logger.error("Verification failed - will retry next sync")
    db.execute("UPDATE customers SET is_synced = 0 WHERE id = ?", (id,))
```

**3. Duplicate on Backend**
```python
try:
    response = api.post('/customer/sync/', data)
except DuplicateError:
    # Backend already has this customer
    # Get existing ID and mark as synced
    existing_id = get_customer_id_by_name(name)
    db.execute("UPDATE customers SET catalytics_id = ?, is_synced = 1", (existing_id, id))
```

---

## Performance Optimization

### Batch Processing
- Fetch 100 customers at a time
- Sync in batches of 50
- Use database transactions

### Caching
- Cache customer name → ID mapping
- Cache product name → ID mapping
- Reduces duplicate checks

### Parallel Processing
- Fetch from companies in parallel (if Tally supports)
- Sync operations can be parallelized

---

## Deployment Checklist

- [ ] Create arasan_gas folder structure
- [ ] Configure .env with 4 company names
- [ ] Set active companies (start with 2)
- [ ] Initialize SQLite database with schema
- [ ] Test customer fetch with duplicate prevention
- [ ] Test product fetch with duplicate prevention
- [ ] Test invoice fetch from multiple companies
- [ ] Test sync to Catalytics with verification
- [ ] Test full cycle: Fetch → Sync → Verify
- [ ] Setup logging and monitoring
- [ ] Create web UI dashboard
- [ ] Document company ownership in logs
- [ ] Train team on duplicate prevention logic
- [ ] Deploy and monitor for 1 week
- [ ] Enable remaining companies if stable

---

## Summary

### Key Features

✅ **Multi-Company Support** - Fetch from 4 Tally companies
✅ **Duplicate Prevention** - First-come-first-served for customers/products
✅ **Company Tracking** - Know which company owns each record
✅ **Data Consistency** - Verification before marking synced
✅ **Flexible Configuration** - Enable/disable companies via .env
✅ **Detailed Logging** - Track duplicates and ownership
✅ **Status Dashboard** - View company statistics in UI

### Critical Success Factors

1. **Uniqueness by Name** - SQLite UNIQUE constraint on customer/product names
2. **Company Ownership** - Always track `tally_company` field
3. **Verification** - Always verify sync before marking complete
4. **Logging** - Log every duplicate skip with company info
5. **Testing** - Test duplicate scenarios thoroughly

---

## Questions for Confirmation

1. **Company Priority**: Should Company 1 always fetch first? Or random order?
2. **Duplicate Handling**: Log only? Or create duplicate report for review?
3. **Product Variants**: If product names match but HSN differs, is it duplicate?
4. **Customer Addresses**: If customer name matches but address differs, is it duplicate?
5. **Sync Frequency**: 30 seconds for invoices, 10 minutes for master data - OK?
6. **Backend Changes**: Do we need to modify Catalytics backend models?
7. **Migration**: Do you want to migrate existing CO_middleware data to arasan_gas?

---

**Ready to start implementation after your confirmation!** 🚀
