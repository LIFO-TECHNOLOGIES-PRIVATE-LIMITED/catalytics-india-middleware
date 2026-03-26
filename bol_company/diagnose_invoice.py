"""
Diagnostic script to check why an invoice is not being fetched.
Helps troubleshoot invoice fetch issues.
"""
import sys
from datetime import datetime
from config import config
from tally_client import TallyClient

def diagnose_invoice(voucher_no, company_name=None):
    """
    Diagnose why a specific invoice is not being fetched.
    
    Args:
        voucher_no: The voucher number to check (e.g., "IO-1724")
        company_name: Optional company name, if not provided will check all active companies
    """
    print(f"\n{'='*70}")
    print(f"INVOICE FETCH DIAGNOSTIC")
    print(f"{'='*70}")
    print(f"Voucher Number: {voucher_no}")
    print(f"{'='*70}\n")
    
    # Get configuration
    try:
        config.reload_from_env()
    except:
        pass
    
    from_date = config.INVOICE_FETCH_START_DATE or datetime.now().strftime('%Y%m%d')
    print(f"📅 Invoice Fetch Start Date: {from_date}")
    print(f"   (Only invoices from {from_date[:4]}-{from_date[4:6]}-{from_date[6:]} onwards will be fetched)\n")
    
    # Get active companies
    active_companies = config.get_active_companies()
    if company_name:
        if company_name not in active_companies:
            print(f"❌ Company '{company_name}' is not in active companies list")
            print(f"   Active companies: {', '.join(active_companies)}")
            return
        companies_to_check = [company_name]
    else:
        companies_to_check = active_companies
    
    print(f"🏢 Checking companies: {', '.join(companies_to_check)}\n")
    
    # Connect to Tally
    tally = TallyClient(config.TALLY_URL)
    print(f"🔌 Tally URL: {config.TALLY_URL}\n")
    
    found = False
    
    for company in companies_to_check:
        print(f"\n{'─'*70}")
        print(f"Checking company: {company}")
        print(f"{'─'*70}")
        
        try:
            # Fetch all invoices (wide date range)
            invoices = tally.get_sales_invoices(company, '20200101', '20991231')
            print(f"✅ Fetched {len(invoices)} total invoices from Tally")
            
            # Find the specific invoice
            target_invoice = None
            for inv in invoices:
                if inv.get('voucher_no') == voucher_no:
                    target_invoice = inv
                    found = True
                    break
            
            if not target_invoice:
                print(f"❌ Invoice {voucher_no} NOT FOUND in {company}")
                continue
            
            print(f"\n✅ INVOICE FOUND in {company}!")
            print(f"\n📋 Invoice Details:")
            print(f"   Voucher No: {target_invoice.get('voucher_no')}")
            print(f"   Date: {target_invoice.get('voucher_date')}")
            print(f"   Customer: {target_invoice.get('customer_name')}")
            print(f"   Amount: {target_invoice.get('total_amount')}")
            
            # Check date filter
            voucher_date = str(target_invoice.get('voucher_date', '')).replace('-', '').strip()
            print(f"\n🔍 Date Filter Check:")
            print(f"   Invoice Date: {voucher_date}")
            print(f"   Start Date: {from_date}")
            
            if voucher_date >= from_date:
                print(f"   ✅ PASS - Invoice date is on or after start date")
            else:
                print(f"   ❌ FAIL - Invoice date is BEFORE start date")
                print(f"   💡 Solution: Update INVOICE_FETCH_START_DATE in .env to {voucher_date} or earlier")
            
            # Check delivery info
            print(f"\n🔍 Delivery Information Check:")
            raw = target_invoice.get('raw_voucher', {}) or {}
            
            checks = {
                'Dispatch/Vehicle fields': bool(raw.get('DISPATCHEDTHROUGH') or raw.get('MOTORVEHICLENO') or raw.get('BASICSHIPPEDBY')),
                'Delivery address': bool(target_invoice.get('delivery_address')),
                'Consignee address': bool((raw.get('CONSIGNEE') or {}).get('ADDRESS') or (raw.get('CONSIGNEE') or {}).get('NAME')),
                'Order No(s) - PARTYORDERNO': bool(raw.get('PARTYORDERNO')),
                'PO Number/Reference': bool(raw.get('PONUMBER') or raw.get('REFERENCE') or raw.get('VOUCHERREFERENCE')),
                'Other References = Delivery': str(raw.get('OTHERREFERENCE', '')).strip().lower() in ('delivery', 'dc', 'delivery challan', 'dispatch'),
                'Terms of delivery/Narration': bool(raw.get('TERMSOFDELIVERY') or 'delivery' in str(raw.get('NARRATION', '')).lower())
            }
            
            has_delivery = any(checks.values())
            
            for check_name, result in checks.items():
                status = "✅ PASS" if result else "❌ FAIL"
                print(f"   {status} - {check_name}")
            
            print(f"\n   Overall: {'✅ HAS DELIVERY INFO' if has_delivery else '❌ NO DELIVERY INFO'}")
            
            if not has_delivery:
                print(f"\n   💡 Solution: Add one of the following to the invoice in Tally:")
                print(f"      - Fill 'Order No(s)' field in Order Details")
                print(f"      - Add vehicle number in Dispatch Details")
                print(f"      - Add consignee address")
                print(f"      - Set 'Other References' to 'Delivery'")
            
            # Show raw voucher fields
            print(f"\n📄 Raw Voucher Fields (for debugging):")
            important_fields = [
                'VOUCHERNUMBER', 'DATE', 'PARTYLEDGERNAME',
                'PARTYORDERNO', 'PARTYORDERDATE', 'PONUMBER', 'REFERENCE',
                'MOTORVEHICLENO', 'DISPATCHEDTHROUGH', 'OTHERREFERENCE',
                'TERMSOFDELIVERY', 'NARRATION'
            ]
            for field in important_fields:
                value = raw.get(field, '')
                if value:
                    print(f"   {field}: {value}")
            
            # Final verdict
            print(f"\n{'='*70}")
            print(f"VERDICT:")
            print(f"{'='*70}")
            
            if voucher_date >= from_date and has_delivery:
                print(f"✅ This invoice SHOULD BE FETCHED")
                print(f"   If it's not appearing, check:")
                print(f"   1. Dashboard logs for errors")
                print(f"   2. SQLite database: bol.sqlite")
                print(f"   3. Restart the dashboard and try 'Full Invoice Fetch' again")
            elif voucher_date < from_date:
                print(f"❌ This invoice will NOT be fetched - DATE TOO OLD")
                print(f"   Update .env: INVOICE_FETCH_START_DATE={voucher_date}")
            elif not has_delivery:
                print(f"❌ This invoice will NOT be fetched - NO DELIVERY INFO")
                print(f"   Add delivery information in Tally (see suggestions above)")
            
        except Exception as e:
            print(f"❌ Error checking {company}: {e}")
            import traceback
            traceback.print_exc()
    
    if not found:
        print(f"\n❌ Invoice {voucher_no} was NOT FOUND in any active company")
        print(f"   Companies checked: {', '.join(companies_to_check)}")
        print(f"\n   Possible reasons:")
        print(f"   1. Invoice number is incorrect")
        print(f"   2. Invoice is in a different company")
        print(f"   3. Invoice has not been saved in Tally yet")
        print(f"   4. Tally connection issue")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python diagnose_invoice.py <voucher_no> [company_name]")
        print("Example: python diagnose_invoice.py IO-1724")
        print("Example: python diagnose_invoice.py IO-1724 'SHREYAA ENTERPRISES'")
        sys.exit(1)
    
    voucher_no = sys.argv[1]
    company_name = sys.argv[2] if len(sys.argv) > 2 else None
    
    diagnose_invoice(voucher_no, company_name)
