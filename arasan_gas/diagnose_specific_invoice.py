"""
Diagnose why a specific invoice is not being fetched
"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import config
import tally_client

logging.basicConfig(
    level=logging.DEBUG,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def diagnose_invoice(voucher_no, company_name):
    """Diagnose why a specific invoice is not being fetched"""
    logger.info(f"\n{'='*60}")
    logger.info(f"DIAGNOSING INVOICE: {voucher_no}")
    logger.info(f"Company: {company_name}")
    logger.info(f"{'='*60}\n")
    
    # Step 1: Check if invoice exists in Tally
    logger.info("Step 1: Fetching invoice from Tally...")
    try:
        voucher = tally_client.get_voucher_by_number(company_name, voucher_no, config.TALLY_URL)
        if not voucher:
            logger.error(f"❌ Invoice {voucher_no} NOT FOUND in Tally")
            return
        
        logger.info(f"✓ Invoice found in Tally")
        logger.info(f"  Voucher Type: {voucher.get('VOUCHERTYPENAME')}")
        logger.info(f"  Date: {voucher.get('DATE')}")
        logger.info(f"  Party: {voucher.get('PARTYLEDGERNAME')}")
    except Exception as e:
        logger.error(f"❌ Error fetching invoice: {e}")
        return
    
    # Step 2: Check date filter
    logger.info("\nStep 2: Checking date filter...")
    invoice_date = voucher.get('DATE', '')
    start_date = config.INVOICE_FETCH_START_DATE
    
    logger.info(f"  Invoice Date: {invoice_date}")
    logger.info(f"  Start Date (from .env): {start_date}")
    
    if start_date:
        # Convert Tally date (YYYYMMDD) to comparable format
        try:
            invoice_date_int = int(invoice_date)
            start_date_int = int(start_date)
            
            if invoice_date_int < start_date_int:
                logger.error(f"❌ Invoice date {invoice_date} is BEFORE start date {start_date}")
                logger.error(f"   This invoice will be SKIPPED by date filter")
                return
            else:
                logger.info(f"✓ Invoice date {invoice_date} is AFTER start date {start_date}")
        except:
            logger.warning(f"⚠ Could not parse dates for comparison")
    else:
        logger.info(f"✓ No start date filter configured (will fetch all dates)")
    
    # Step 3: Check delivery info
    logger.info("\nStep 3: Checking delivery information...")
    
    # Check all delivery-related fields
    checks = {
        'OTHERREFERENCE': voucher.get('OTHERREFERENCE', ''),
        'VOUCHERREFERENCE': voucher.get('VOUCHERREFERENCE', ''),
        'BASICORDERREF': voucher.get('BASICORDERREF', ''),
        'PARTYORDERNO': voucher.get('PARTYORDERNO', ''),
        'ORDERREF': voucher.get('ORDERREF', ''),
        'TERMSOFDELIVERY': voucher.get('TERMSOFDELIVERY', ''),
        'NARRATION': voucher.get('NARRATION', ''),
        'CONSIGNEE': voucher.get('CONSIGNEE', {}),
    }
    
    logger.info("  Checking delivery-related fields:")
    has_delivery = False
    
    for field, value in checks.items():
        if value:
            logger.info(f"    {field}: {value}")
            
            # Check if this field indicates delivery
            if field in ['OTHERREFERENCE', 'VOUCHERREFERENCE']:
                value_lower = str(value).strip().lower()
                if value_lower in ('delivery', 'dc', 'delivery challan', 'dispatch'):
                    logger.info(f"      ✓ This field indicates DELIVERY")
                    has_delivery = True
            
            if field == 'CONSIGNEE' and isinstance(value, dict) and value.get('ADDRESS'):
                logger.info(f"      ✓ Consignee address found")
                has_delivery = True
            
            if field in ['PARTYORDERNO', 'ORDERREF', 'BASICORDERREF']:
                logger.info(f"      ✓ Order reference found")
                has_delivery = True
        else:
            logger.debug(f"    {field}: (empty)")
    
    # Check inventory for godown
    inventory = voucher.get('INVENTORY', [])
    if inventory:
        logger.info(f"\n  Checking inventory items ({len(inventory)} items):")
        for idx, item in enumerate(inventory[:3], 1):  # Show first 3 items
            godown = item.get('GODOWNNAME', '')
            if godown:
                logger.info(f"    Item {idx}: GODOWNNAME = {godown}")
                has_delivery = True
    
    # Final verdict
    logger.info(f"\n{'='*60}")
    if has_delivery:
        logger.info(f"✓ VERDICT: Invoice HAS delivery information")
        logger.info(f"  This invoice SHOULD BE FETCHED")
    else:
        logger.error(f"❌ VERDICT: Invoice DOES NOT have delivery information")
        logger.error(f"  This invoice will be SKIPPED")
        logger.error(f"\n  To fix this, add one of the following in Tally:")
        logger.error(f"    1. Set 'Other References' field to 'Delivery'")
        logger.error(f"    2. Add a consignee address")
        logger.error(f"    3. Add an order number")
        logger.error(f"    4. Add a godown/location to inventory items")
    logger.info(f"{'='*60}\n")
    
    # Step 4: Show full voucher data
    logger.info("\nStep 4: Full voucher data (for debugging):")
    import json
    logger.info(json.dumps(voucher, indent=2, default=str))


if __name__ == '__main__':
    # Diagnose the invoice from the screenshot
    voucher_no = "IO-1724"  # From the screenshot
    company_name = "SHREYAA ENTERPRISES"  # From the screenshot
    
    # Allow command line arguments
    if len(sys.argv) > 1:
        voucher_no = sys.argv[1]
    if len(sys.argv) > 2:
        company_name = sys.argv[2]
    
    diagnose_invoice(voucher_no, company_name)
