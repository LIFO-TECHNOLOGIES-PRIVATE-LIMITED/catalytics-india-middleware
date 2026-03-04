# Customer Name Extraction Fix

## Issue

Customers fetched from Tally were showing incorrect names like "7" instead of actual customer names.

**Example of the issue:**
```json
{
  "guid": "9ccce596-2b2a-4de5-9820-cf385b6e1d39-00007e07",
  "name": "7",
  "parent_group": "Debtors Single Parties",
  "gstin": "33ABAPM7460P1ZB",
  "address": "PH 82707 21218, MR SARAVANA MUTHU"
}
```

The actual customer name "MR SARAVANA MUTHU" was visible in the address field, but the name field showed "7".

## Root Cause

The issue occurred in two places in `tally_client.py`:

### 1. Invoice Customer Extraction (Line ~1448)

When extracting customer names from sales invoices, if `PARTYLEDGERNAME` was empty, the code fell back to taking the FIRST ledger entry from `LEDGERENTRIES`:

```python
if not cust and v.get('LEDGERENTRIES'):
    cust = (v.get('LEDGERENTRIES')[0].get('LEDGERNAME') or '').strip() or 'UNKNOWN'
```

**Problem:** The first ledger entry might be a tax ledger (CGST, SGST, IGST) or other non-customer ledger with a numeric name like "7".

### 2. Ledger Parsing (Line ~234)

When parsing ledger XML from Tally, the code extracted the NAME from the LEDGER element's NAME attribute, but then child elements would overwrite it:

```python
# Extract NAME from attribute
ledger_name = ledger.get("NAME", "")
if ledger_name:
    data["NAME"] = ledger_name

# Parse child elements
for child in ledger:
    data[child.tag.upper()] = (child.text or "").strip()  # This overwrites NAME!
```

**Problem:** If Tally XML contains a child `<NAME>7</NAME>` element, it overwrites the correct NAME attribute value.

## Solution

### Fix 1: Smart Ledger Entry Selection

Updated invoice customer extraction to skip tax-related and numeric ledgers:

```python
if not cust and v.get('LEDGERENTRIES'):
    # Find the customer ledger (skip tax ledgers)
    for ledger_entry in v.get('LEDGERENTRIES', []):
        ledger_name = (ledger_entry.get('LEDGERNAME') or '').strip()
        ledger_name_upper = ledger_name.upper()
        # Skip tax-related ledgers
        if any(tax_keyword in ledger_name_upper for tax_keyword in 
               ['CGST', 'SGST', 'IGST', 'GST', 'TAX', 'CESS', 'DUTY', 'OUTPUT', 'INPUT']):
            continue
        # Skip numeric-only names (like "7")
        if ledger_name.isdigit():
            continue
        # This is likely the customer ledger
        cust = ledger_name
        break
    if not cust:
        cust = 'UNKNOWN'
```

### Fix 2: Protect NAME Attribute

Updated ledger parsing to skip NAME child elements after extracting from attribute:

```python
# Extract NAME from attribute
ledger_name = ledger.get("NAME", "")
if ledger_name:
    data["NAME"] = ledger_name

# Parse child elements
for child in ledger:
    if child.tag.endswith(".LIST"):
        continue
    # Skip NAME child element - we already got it from the attribute
    if child.tag.upper() == "NAME":
        continue
    data[child.tag.upper()] = (child.text or "").strip()
```

## Files Modified

- `catalytics-india-middleware/arasan_gas/tally_client.py`
  - Line ~1448: Smart ledger entry selection for invoice customer extraction
  - Line ~234: Skip NAME child element in ledger parsing (main parser)
  - Line ~267: Skip NAME child element in ledger parsing (fallback parser)

## Testing

Two test scripts were created to verify the fix:

### 1. Test Customer Extraction from Invoices
```bash
cd catalytics-india-middleware/arasan_gas
python test_customer_extraction.py
```

This tests customer name extraction from sales invoices.

### 2. Test Sundry Debtors Fetching
```bash
cd catalytics-india-middleware/arasan_gas
python test_sundry_debtors.py
```

This tests customer fetching from Sundry Debtors ledgers.

## Expected Results

After the fix:
- Customer names should be extracted correctly from invoices
- Sundry Debtors should show proper customer names, not numeric values
- Tax ledgers should not be mistaken for customer ledgers
- NAME attribute from LEDGER element should not be overwritten by child elements

## Deployment

This fix is included in the next middleware release. To deploy:

1. Stop the middleware service
2. Replace `tally_client.py` with the fixed version
3. Restart the middleware service
4. Run test scripts to verify the fix

## Related Issues

- Customer name showing as "7" instead of actual name
- Invoice sync failing due to incorrect customer names
- Master data sync issues with numeric customer names
