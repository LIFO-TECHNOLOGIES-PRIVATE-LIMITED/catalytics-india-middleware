"""
Test what's actually in your current Tally server
"""
import logging
import sys
import os

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import tally_client
import config as cfg

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_current_tally():
    """Test what's in the current Tally"""
    
    # Load config
    cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))
    tally_url = cfg.get_env("TALLY_URL")
    company_name = cfg.get_env("TALLY_COMPANY")
    
    logger.info("=" * 80)
    logger.info("Testing Current Tally Server")
    logger.info("URL: %s", tally_url)
    logger.info("Company: %s", company_name)
    logger.info("=" * 80)
    
    # Step 1: Get companies
    logger.info("\n=== STEP 1: Companies ===")
    try:
        companies = tally_client.get_companies(tally_url)
        logger.info("Found %d companies:", len(companies))
        for comp in companies:
            logger.info("  - %s", comp.get("name"))
    except Exception as e:
        logger.error("Failed to get companies: %s", e)
        return
    
    # Step 2: Get ALL ledgers (not filtered)
    logger.info("\n=== STEP 2: ALL Ledgers ===")
    try:
        all_ledgers = tally_client.get_ledgers(company_name, tally_url)
        logger.info("Found %d total ledgers", len(all_ledgers))
        
        # Group by parent
        by_parent = {}
        for ledger in all_ledgers:
            parent = (ledger.get("PARENT") or "").strip().lower()
            if parent not in by_parent:
                by_parent[parent] = []
            by_parent[parent].append(ledger.get("NAME", ""))
        
        logger.info("\nLedgers by parent group:")
        for parent, names in sorted(by_parent.items()):
            if parent:
                logger.info("  %s: %d ledgers", parent, len(names))
                for name in names[:5]:  # Show first 5
                    logger.info("    - %s", name)
                if len(names) > 5:
                    logger.info("    ... and %d more", len(names) - 5)
    except Exception as e:
        logger.error("Failed to get ledgers: %s", e)
    
    # Step 3: Filter for Sundry Debtors (customers)
    logger.info("\n=== STEP 3: Sundry Debtors (Customers) ===")
    try:
        customer_groups = ["sundry debtors", "debtors", "sundry debtors (current)", "receivables"]
        customer_ledgers = []
        for ledger in all_ledgers:
            parent = (ledger.get("PARENT") or "").strip().lower()
            if any(group in parent for group in customer_groups):
                customer_ledgers.append(ledger)
        
        logger.info("Found %d customer ledgers:", len(customer_ledgers))
        for ledger in customer_ledgers:
            logger.info("  - %s (Parent: %s)", 
                       ledger.get("NAME", "N/A"),
                       ledger.get("PARENT", "N/A"))
    except Exception as e:
        logger.error("Failed to filter customers: %s", e)
    
    # Step 4: Get stock items (products)
    logger.info("\n=== STEP 4: Stock Items (Products) ===")
    try:
        products = tally_client.get_stock_items(company_name, tally_url)
        logger.info("Found %d products", len(products))
        for product in products[:5]:  # Show first 5
            logger.info("  - %s", product.get("NAME", "N/A"))
        if len(products) > 5:
            logger.info("  ... and %d more", len(products) - 5)
    except Exception as e:
        logger.error("Failed to get products: %s", e)
    
    # Step 5: Get ALL vouchers
    logger.info("\n=== STEP 5: ALL Vouchers ===")
    try:
        xml = f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>AllVouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVCURRENTCOMPANY>{company_name}</SVCURRENTCOMPANY>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION ISMODIFY="No" NAME="AllVouchers">
            <TYPE>Voucher</TYPE>
            <NATIVEMETHOD>VoucherTypeName</NATIVEMETHOD>
            <NATIVEMETHOD>VoucherNumber</NATIVEMETHOD>
            <NATIVEMETHOD>Date</NATIVEMETHOD>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
"""
        resp = tally_client.send_request(xml, tally_url)
        
        # Parse vouchers
        import xml.etree.ElementTree as ET
        root = ET.fromstring(resp)
        vouchers = []
        for v in root.findall(".//VOUCHER"):
            vt_name = v.findtext("VOUCHERTYPENAME") or ""
            vch_no = v.findtext("VOUCHERNUMBER") or ""
            date = v.findtext("DATE") or ""
            if vt_name or vch_no:
                vouchers.append({
                    "type": vt_name.strip(),
                    "number": vch_no.strip(),
                    "date": date.strip()
                })
        
        logger.info("Found %d total vouchers", len(vouchers))
        
        # Group by type
        by_type = {}
        for v in vouchers:
            vt = v["type"]
            if vt not in by_type:
                by_type[vt] = []
            by_type[vt].append(v)
        
        logger.info("\nVouchers by type:")
        for vt, vlist in sorted(by_type.items()):
            logger.info("  %s: %d vouchers", vt, len(vlist))
            for v in vlist[:3]:  # Show first 3
                logger.info("    - #%s (Date: %s)", v["number"], v["date"])
            if len(vlist) > 3:
                logger.info("    ... and %d more", len(vlist) - 3)
        
        # Filter for delivery notes
        dc_vouchers = [v for v in vouchers if any(
            keyword in v["type"].lower() 
            for keyword in ["delivery", "challan", "dc", "delv"]
        )]
        
        logger.info("\n=== Delivery Notes (DCs) ===")
        logger.info("Found %d DCs:", len(dc_vouchers))
        for v in dc_vouchers:
            logger.info("  - Type: %s, Number: %s, Date: %s", 
                       v["type"], v["number"], v["date"])
    except Exception as e:
        logger.error("Failed to get vouchers: %s", e)
    
    logger.info("\n=== Test Complete ===")

if __name__ == "__main__":
    test_current_tally()
