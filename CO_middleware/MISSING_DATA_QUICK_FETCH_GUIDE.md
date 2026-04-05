# CO_Middleware: Quick Fetch for Missing Products & Customers

## Problem

When DC fetch encounters missing products or customers, the current flow:
1. Skips the DC (logs it as missing)
2. Waits for manual intervention or next sync cycle
3. Takes time to resolve

**Result**: DCs are not synced until missing data is manually added.

## Solution: Quick Auto-Fetch

Automatically fetch missing products and customers from Tally and save them to the database during DC fetch.

---

## Implementation Steps

### Step 1: Enable Auto-Fetch in `.env`

```env
# Auto-fetch missing products from Tally when DC fetch encounters them
AUTO_FETCH_PRODUCTS=true

# Auto-fetch missing customers from Tally when DC fetch encounters them
AUTO_FETCH_CUSTOMERS=true
```

**Current Status**: ✅ Already configured in `.env`

---

### Step 2: Integrate with fetch_invoices.py

The middleware needs to call the backend API when missing data is detected.

#### Add this import at the top of `fetch_invoices.py`:

```python
import requests
from config import CATALYTICS_API_BASE_URL, ENTITY_ID, TALLY_URL, TALLY_COMPANY
```

#### Add this function to handle missing data:

```python
def _fetch_missing_data_from_backend(missing_products: List[str], missing_customers: List[str]) -> bool:
    """
    Call backend API to fetch missing products and customers from Tally.
    
    Returns:
        True if fetch was successful or skipped, False if error occurred
    """
    if not missing_products and not missing_customers:
        return True
    
    try:
        api_url = f"{CATALYTICS_API_BASE_URL.rstrip('/')}/import/fetch-missing-data/"
        
        payload = {
            "entity_id": ENTITY_ID,
            "tally_url": TALLY_URL,
            "company": TALLY_COMPANY,
            "missing_products": missing_products,
            "missing_customers": missing_customers
        }
        
        logger.info(f"[AUTO-FETCH] Calling backend API to fetch missing data...")
        logger.info(f"[AUTO-FETCH] Missing products: {len(missing_products)}, customers: {len(missing_customers)}")
        
        response = requests.post(api_url, json=payload, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"[AUTO-FETCH] ✓ Success: {result.get('message', 'Data fetched')}")
            logger.info(f"[AUTO-FETCH] Products fetched: {result['results']['products_fetched']}, "
                       f"Customers fetched: {result['results']['customers_fetched']}")
            return True
        else:
            logger.warning(f"[AUTO-FETCH] ✗ API returned status {response.status_code}: {response.text}")
            return False
            
    except requests.Timeout:
        logger.error(f"[AUTO-FETCH] ✗ API request timeout (30s)")
        return False
    except Exception as e:
        logger.error(f"[AUTO-FETCH] ✗ Error calling API: {e}")
        return False
```

#### Modify the DC fetch loop to call this function:

In the `run_once()` function, when you detect missing products/customers:

```python
# Collect missing products and customers
missing_products = []
missing_customers = []

for dc in delivery_notes:
    # ... existing code ...
    
    # Check for missing customer
    if not customer_exists:
        missing_customers.append(customer_name)
        continue
    
    # Check for missing products
    for item in items:
        if not product_exists:
            missing_products.append(item_name)
    
    if missing_products:
        continue

# After collecting all missing data, try to fetch them
if missing_products or missing_customers:
    logger.info(f"[DC FETCH] Found missing data - attempting auto-fetch...")
    success = _fetch_missing_data_from_backend(missing_products, missing_customers)
    
    if success:
        logger.info(f"[DC FETCH] Missing data fetched successfully - retrying DC fetch...")
        # Retry the DC fetch with the newly fetched data
    else:
        logger.warning(f"[DC FETCH] Failed to fetch missing data - will retry in next cycle")
```

---

### Step 3: Quick Fetch Strategies

#### Strategy 1: Immediate Fetch (Recommended)
- When missing data is detected, immediately call the backend API
- Wait for response (max 30 seconds)
- If successful, retry DC fetch
- If failed, log and continue (will retry next cycle)

**Pros**: Fast resolution, DCs synced immediately
**Cons**: Adds 30s delay if API is slow

#### Strategy 2: Batch Fetch
- Collect all missing data during DC fetch
- After DC fetch completes, call API once with all missing items
- Next DC fetch cycle will use the newly fetched data

**Pros**: Faster DC fetch, single API call
**Cons**: One cycle delay before DCs are synced

#### Strategy 3: Background Fetch
- Detect missing data and log it
- Trigger a background task to fetch missing data
- Continue DC fetch without waiting
- Next cycle will use the newly fetched data

**Pros**: No delay to DC fetch
**Cons**: Requires background task infrastructure

---

## Configuration

### Environment Variables

```env
# Enable/disable auto-fetch
AUTO_FETCH_PRODUCTS=true
AUTO_FETCH_CUSTOMERS=true

# Backend API URL (already configured)
CATALYTICS_API_BASE_URL=http://localhost:8000/

# Entity and company info (already configured)
ENTITY_ID=29
TALLY_COMPANY=CHENNAI OXYGEN
TALLY_URL=http://localhost:9000/
```

### Timeout Settings

```python
# In fetch_invoices.py
API_TIMEOUT_SECONDS = 30  # Max wait for API response
API_RETRY_COUNT = 1       # Number of retries if API fails
```

---

## How It Works

### Current Flow (Without Auto-Fetch)
```
DC Fetch Start
    ↓
Check Customer Exists
    ↓ (Not Found)
Skip DC, Log Missing
    ↓
DC Fetch End
    ↓
Manual: Add Customer to Tally
    ↓
Next Cycle: Fetch DC Again
```

### New Flow (With Auto-Fetch)
```
DC Fetch Start
    ↓
Check Customer Exists
    ↓ (Not Found)
Collect Missing Data
    ↓
Call Backend API
    ↓ (Success)
Save Customer to DB
    ↓
Retry DC Fetch
    ↓
DC Synced Successfully
```

---

## API Endpoints

### Fetch Missing Data
**Endpoint**: `POST /import/fetch-missing-data/`

**Request**:
```json
{
    "entity_id": 29,
    "tally_url": "http://localhost:9000",
    "company": "CHENNAI OXYGEN",
    "missing_products": ["OXYGEN 50 KG", "NITROGEN 40 LIT"],
    "missing_customers": ["Customer A", "Customer B"]
}
```

**Response**:
```json
{
    "status": "success",
    "message": "Fetched 2 products and 2 customers",
    "results": {
        "products_fetched": 2,
        "products_failed": 0,
        "customers_fetched": 2,
        "customers_failed": 0,
        "products_skipped": 0,
        "customers_skipped": 0
    }
}
```

---

## Logging

All auto-fetch operations are logged with `[AUTO-FETCH]` prefix:

```
[2025-01-15 10:30:45] [INFO] [DC FETCH] Found missing data - attempting auto-fetch...
[2025-01-15 10:30:45] [INFO] [AUTO-FETCH] Calling backend API to fetch missing data...
[2025-01-15 10:30:45] [INFO] [AUTO-FETCH] Missing products: 2, customers: 1
[2025-01-15 10:30:46] [INFO] [AUTO-FETCH] ✓ Success: Fetched 2 products and 1 customer
[2025-01-15 10:30:46] [INFO] [AUTO-FETCH] Products fetched: 2, Customers fetched: 1
[2025-01-15 10:30:46] [INFO] [DC FETCH] Missing data fetched successfully - retrying DC fetch...
```

**Log Files**:
- `logs/dc_fetch.log` - All DC fetch operations
- `logs/dc_fetch_errors.log` - Errors only

---

## Error Handling

### API Timeout
```
[AUTO-FETCH] ✗ API request timeout (30s)
→ Log warning and continue
→ Will retry in next DC fetch cycle
```

### API Error
```
[AUTO-FETCH] ✗ API returned status 500: Internal Server Error
→ Log error and continue
→ Will retry in next DC fetch cycle
```

### Network Error
```
[AUTO-FETCH] ✗ Error calling API: Connection refused
→ Log error and continue
→ Will retry in next DC fetch cycle
```

---

## Performance Optimization

### 1. Batch Missing Data
Instead of fetching one product at a time, collect all missing items and fetch in one API call.

```python
# Collect all missing data first
missing_products = set()
missing_customers = set()

for dc in delivery_notes:
    if not customer_exists:
        missing_customers.add(customer_name)
    for item in items:
        if not product_exists:
            missing_products.add(item_name)

# Then fetch all at once
if missing_products or missing_customers:
    _fetch_missing_data_from_backend(list(missing_products), list(missing_customers))
```

### 2. Cache Results
After fetching, cache the results to avoid re-fetching in the same cycle.

```python
# Cache fetched data
fetched_products = {}
fetched_customers = {}

# After API call
for product in response['results']['products_fetched']:
    fetched_products[product['name']] = product

# Use cache for subsequent checks
if item_name in fetched_products:
    # Use cached data
```

### 3. Parallel Fetch
If you have many missing items, fetch products and customers in parallel.

```python
import concurrent.futures

def _fetch_products_only(products):
    # Fetch only products
    pass

def _fetch_customers_only(customers):
    # Fetch only customers
    pass

# Fetch in parallel
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
    product_future = executor.submit(_fetch_products_only, missing_products)
    customer_future = executor.submit(_fetch_customers_only, missing_customers)
    
    product_result = product_future.result()
    customer_result = customer_future.result()
```

---

## Testing

### Test 1: Verify Auto-Fetch Works
```bash
# 1. Ensure .env has AUTO_FETCH_PRODUCTS=true and AUTO_FETCH_CUSTOMERS=true
# 2. Run DC fetch with a DC that has missing products/customers
# 3. Check logs for [AUTO-FETCH] messages
# 4. Verify DC is synced successfully
```

### Test 2: Verify Error Handling
```bash
# 1. Stop the backend API
# 2. Run DC fetch with missing data
# 3. Verify error is logged
# 4. Verify DC fetch continues (doesn't crash)
# 5. Start backend API
# 6. Run DC fetch again - should succeed
```

### Test 3: Verify Performance
```bash
# 1. Measure time with auto-fetch disabled
# 2. Measure time with auto-fetch enabled
# 3. Compare results
```

---

## Troubleshooting

### Issue: Auto-fetch not working
**Solution**: 
1. Check `.env` has `AUTO_FETCH_PRODUCTS=true` and `AUTO_FETCH_CUSTOMERS=true`
2. Check backend API is running: `curl http://localhost:8000/import/missing-data-status/`
3. Check logs for `[AUTO-FETCH]` messages
4. Verify `CATALYTICS_API_BASE_URL` is correct in `.env`

### Issue: API timeout
**Solution**:
1. Increase timeout: Change `timeout=30` to `timeout=60` in the function
2. Check backend API performance
3. Check network connectivity

### Issue: Missing data not being fetched
**Solution**:
1. Check if `AUTO_FETCH_PRODUCTS` or `AUTO_FETCH_CUSTOMERS` is set to `false`
2. Check backend logs for errors
3. Verify Tally is running and accessible

---

## Summary

✅ **Quick Fetch Strategy**: Call backend API immediately when missing data is detected
✅ **Automatic Resolution**: No manual intervention needed
✅ **Fast Sync**: DCs synced in the same cycle
✅ **Error Handling**: Graceful fallback if API fails
✅ **Logging**: Detailed logs for troubleshooting
✅ **Performance**: Batch fetch for efficiency

**Next Steps**:
1. Add the `_fetch_missing_data_from_backend()` function to `fetch_invoices.py`
2. Integrate it into the DC fetch loop
3. Test with missing products/customers
4. Monitor logs and performance
