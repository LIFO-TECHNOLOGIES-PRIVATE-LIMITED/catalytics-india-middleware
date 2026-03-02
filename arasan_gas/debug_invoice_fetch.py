"""
Debug: Fetch raw invoices from Tally and show what fields we get
and why _has_delivery_info passes or fails.
Run: py debug_invoice_fetch.py
"""
import sys, json
sys.path.insert(0, '.')

from config import config
from tally_client import TallyClient

tally = TallyClient(config.TALLY_URL)
from_date = config.INVOICE_FETCH_START_DATE or '20260101'

print(f"=== DEBUG INVOICE FETCH ===")
print(f"Tally URL: {config.TALLY_URL}")
print(f"From date: {from_date}")
print(f"Companies: {config.get_active_companies()}")
print()

for company in config.get_active_companies():
    print(f"\n{'='*60}")
    print(f"Company: {company}")
    print('='*60)
    try:
        invoices = tally.get_sales_invoices(company, from_date, '20991231')
        print(f"  Total invoices from Tally: {len(invoices)}")

        for i, inv in enumerate(invoices[:10]):  # show first 10
            raw = inv.get('raw_voucher', {})
            print(f"\n  [{i+1}] Voucher: {inv.get('voucher_no')}  Date: {inv.get('voucher_date')}  Customer: {inv.get('customer_name')}")
            print(f"       delivery_address : '{inv.get('delivery_address', '')}'")
            print(f"       DISPATCHEDTHROUGH: '{raw.get('DISPATCHEDTHROUGH', '')}'")
            print(f"       MOTORVEHICLENO   : '{raw.get('MOTORVEHICLENO', '')}'")
            print(f"       VEHICLENO        : '{raw.get('VEHICLENO', '')}'")
            print(f"       PARTYORDERNO     : '{raw.get('PARTYORDERNO', '')}'")
            print(f"       PONUMBER         : '{raw.get('PONUMBER', '')}'")
            print(f"       REFERENCE        : '{raw.get('REFERENCE', '')}'")
            print(f"       OTHERREFERENCE   : '{raw.get('OTHERREFERENCE', '')}'")
            print(f"       VOUCHERREFERENCE : '{raw.get('VOUCHERREFERENCE', '')}'")
            consignee = raw.get('CONSIGNEE', {})
            print(f"       CONSIGNEE        : '{consignee}'")
            print(f"       TERMSOFDELIVERY  : '{raw.get('TERMSOFDELIVERY', '')}'")

            # Check what _has_delivery_info returns
            has_disp  = bool(raw.get('DISPATCHEDTHROUGH') or raw.get('MOTORVEHICLENO') or raw.get('BASICSHIPPEDBY'))
            has_deliv = bool(inv.get('delivery_address'))
            has_cons  = isinstance(consignee, dict) and bool(consignee.get('ADDRESS') or consignee.get('NAME'))
            has_order = bool(raw.get('PARTYORDERNO'))
            has_po    = bool(raw.get('PONUMBER') or raw.get('REFERENCE') or raw.get('VOUCHERREFERENCE'))
            other_ref = str(raw.get('OTHERREFERENCE') or raw.get('VOUCHERREFERENCE') or '').strip().lower()
            has_other = other_ref in ('delivery', 'dc', 'delivery challan', 'dispatch')

            passes = any([has_disp, has_deliv, has_cons, has_order, has_po, has_other])
            print(f"       FILTER RESULT    : {'PASS (will save)' if passes else 'SKIP (no delivery info)'}")
            print(f"         - dispatch      : {has_disp}")
            print(f"         - delivery_addr : {has_deliv}")
            print(f"         - consignee     : {has_cons}")
            print(f"         - order_no      : {has_order}")
            print(f"         - po/ref        : {has_po}")
            print(f"         - other_ref     : {has_other}")

        # Count pass/fail for all
        from fetch_invoices import _has_delivery_info
        passed = sum(1 for inv in invoices if _has_delivery_info(inv))
        print(f"\n  SUMMARY: {passed}/{len(invoices)} invoices pass delivery filter")

    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback; traceback.print_exc()
