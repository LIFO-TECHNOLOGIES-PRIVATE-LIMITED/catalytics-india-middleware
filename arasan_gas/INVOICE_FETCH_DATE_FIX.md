# Invoice Fetch Date Fix

## Problem
Invoice IO-1724 (dated 8-Mar-25 / 20250308) was not being fetched because the `INVOICE_FETCH_START_DATE` was set to 20260228 (Feb 28, 2026).

## Solution Applied
Updated `.env` file to fetch invoices from January 1, 2025 onwards:

```
INVOICE_FETCH_START_DATE=20250101  # Changed from 20260228
```

## What This Means
- The middleware will now fetch ALL invoices from January 1, 2025 onwards
- Your invoice IO-1724 (March 8, 2025) will now be included
- Any other 2025 invoices will also be fetched

## Next Steps

### 1. Add Delivery Information to Invoice IO-1724
The invoice still needs delivery information to be fetched. Add at least ONE of these in Tally:

**Option A: Add Order Number (Recommended)**
1. Open invoice IO-1724 in Tally
2. Press F12 (Configure)
3. Enable "Order Details"
4. Fill in "Order No(s)" field (e.g., "po12")
5. Optionally fill in "Date" field
6. Save the invoice

**Option B: Add Vehicle Number**
1. Open invoice IO-1724 in Tally
2. Press F12 (Configure)
3. Enable "Dispatch Details"
4. Fill in "Motor Vehicle No" field (e.g., "TN 98 RS 2345")
5. Save the invoice

**Option C: Set Other References**
1. Open invoice IO-1724 in Tally
2. In "Other References" field, type "Delivery"
3. Save the invoice

### 2. Fetch the Invoice
After adding delivery information:
1. Open middleware dashboard: http://localhost:5000
2. Click "Full Invoice Fetch" button
3. Check logs for: "✅ Saved new invoice: IO-1724"

### 3. Verify in Database
```bash
cd C:\Github\catalytics-india-middleware\arasan_gas
sqlite3 arasan_gas.sqlite

SELECT voucher_no, customer_name, voucher_date, is_synced 
FROM invoices 
WHERE voucher_no = 'IO-1724';

.quit
```

## Diagnostic Tool
To check if the invoice will be fetched now:

```bash
cd C:\Github\catalytics-india-middleware\arasan_gas
python diagnose_invoice.py IO-1724
```

This will show:
- ✅ Date filter status (should now PASS)
- ❌ Delivery info status (will FAIL until you add delivery info)
- 💡 Specific suggestions to fix

## Important Notes

1. **No restart needed**: The dashboard automatically reloads `.env` on each fetch
2. **Date filter now passes**: Invoice date (20250308) >= Start date (20250101) ✅
3. **Delivery info still required**: Must add Order No, Vehicle No, or Other References
4. **All 2025 invoices**: Any invoice from 2025 onwards will now be fetched (if it has delivery info)

## Files Modified
- ✅ `catalytics-india-middleware/arasan_gas/.env`
- ✅ `catalytics-india-middleware/arasan_gas/.env.example`
