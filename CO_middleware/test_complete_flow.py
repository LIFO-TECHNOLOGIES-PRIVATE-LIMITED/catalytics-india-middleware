"""
Complete test of fetch flow: customers, products, DCs
"""
import logging
import sys
import os
import sqlite3

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import tally_client
import config as cfg
import db

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_complete_flow():
    """Test complete fetch flow"""
    
    # Load config
    cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))
    tally_url = cfg.get_env("TALLY_URL")
    company_name = cfg.get_env("TALLY_COMPANY")
    db_path = cfg.get_env("TALLY_DB_PATH")
    
    logger.info("=" * 80)
    logger.info("COMPLETE FLOW TEST")
    logger.info("=" * 80)
    logger.info("Tally URL: %s", tally_url)
    logger.info("Company: %s", company_name)
    logger.info("Database: %s", db_path)
    logger.info("")
    
    # ========================================================================
    # STEP 1: Fetch from Tally
    # ========================================================================
    logger.info("=" * 80)
    logger.info("STEP 1: FETCH FROM TALLY")
    logger.info("=" * 80)
    
    # 1a. Customers
    logger.info("\n--- 1a. Fetching Customers ---")
    try:
        all_ledgers = tally_client.get_ledgers(company_name, tally_url)
        customer_groups = ["sundry debtors", "debtors", "sundry debtors (current)", "receivables"]
        customers = [l for l in all_ledgers 
                    if any(g in (l.get("PARENT") or "").lower() for g in customer_groups)]
        logger.info("✓ Fetched %d customers from Tally:", len(customers))
        for c in customers:
            logger.info("  - %s", c.get("NAME"))
    except Exception as e:
        logger.error("✗ Customer fetch failed: %s", e)
        customers = []
    
    # 1b. Products
    logger.info("\n--- 1b. Fetching Products ---")
    try:
        products = tally_client.get_stock_items(company_name, tally_url)
        logger.info("✓ Fetched %d products from Tally:", len(products))
        for p in products[:5]:
            logger.info("  - %s", p.get("NAME"))
        if len(products) > 5:
            logger.info("  ... and %d more", len(products) - 5)
    except Exception as e:
        logger.error("✗ Product fetch failed: %s", e)
        products = []
    
    # 1c. DCs
    logger.info("\n--- 1c. Fetching DCs ---")
    try:
        dcs = tally_client.get_delivery_notes(company_name, tally_url, "20200101", "20991231")
        logger.info("✓ Fetched %d DCs from Tally:", len(dcs))
        for dc in dcs:
            logger.info("  - DC #%s (Date: %s, Party: %s)", 
                       dc.get("VOUCHERNUMBER"),
                       dc.get("DATE"),
                       dc.get("PARTYLEDGERNAME"))
    except Exception as e:
        logger.error("✗ DC fetch failed: %s", e)
        dcs = []
    
    # ========================================================================
    # STEP 2: Check Database
    # ========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("STEP 2: CHECK DATABASE")
    logger.info("=" * 80)
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # 2a. Customers in DB
        logger.info("\n--- 2a. Customers in Database ---")
        db_customers = conn.execute("SELECT name FROM ledgers ORDER BY name").fetchall()
        logger.info("Database has %d customers:", len(db_customers))
        for c in db_customers:
            logger.info("  - %s", c["name"])
        
        # 2b. Products in DB
        logger.info("\n--- 2b. Products in Database ---")
        db_products = conn.execute("SELECT name FROM stock_items ORDER BY name").fetchall()
        logger.info("Database has %d products:", len(db_products))
        for p in db_products[:5]:
            logger.info("  - %s", p["name"])
        if len(db_products) > 5:
            logger.info("  ... and %d more", len(db_products) - 5)
        
        # 2c. DCs in DB
        logger.info("\n--- 2c. DCs in Database ---")
        db_dcs = conn.execute("""
            SELECT voucher_number, date, party_name 
            FROM delivery_notes 
            ORDER BY date DESC
        """).fetchall()
        logger.info("Database has %d DCs:", len(db_dcs))
        for dc in db_dcs:
            logger.info("  - DC #%s (Date: %s, Party: %s)", 
                       dc["voucher_number"],
                       dc["date"],
                       dc["party_name"])
        
        conn.close()
        
    except Exception as e:
        logger.error("✗ Database check failed: %s", e)
    
    # ========================================================================
    # STEP 3: Compare Tally vs Database
    # ========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("STEP 3: COMPARISON (Tally vs Database)")
    logger.info("=" * 80)
    
    logger.info("\nCustomers:")
    logger.info("  Tally: %d", len(customers))
    logger.info("  Database: %d", len(db_customers))
    if len(customers) != len(db_customers):
        logger.warning("  ⚠ MISMATCH! Need to fetch customers")
    else:
        logger.info("  ✓ Match")
    
    logger.info("\nProducts:")
    logger.info("  Tally: %d", len(products))
    logger.info("  Database: %d", len(db_products))
    if len(products) != len(db_products):
        logger.warning("  ⚠ MISMATCH! Need to fetch products")
    else:
        logger.info("  ✓ Match")
    
    logger.info("\nDCs:")
    logger.info("  Tally: %d", len(dcs))
    logger.info("  Database: %d", len(db_dcs))
    if len(dcs) != len(db_dcs):
        logger.warning("  ⚠ MISMATCH! Need to fetch DCs")
    else:
        logger.info("  ✓ Match")
    
    # ========================================================================
    # STEP 4: Recommendations
    # ========================================================================
    logger.info("\n" + "=" * 80)
    logger.info("STEP 4: RECOMMENDATIONS")
    logger.info("=" * 80)
    
    needs_action = False
    
    if len(customers) != len(db_customers):
        logger.info("\n📋 ACTION NEEDED: Fetch Customers")
        logger.info("   Run: python fetch_customers.py")
        logger.info("   Or click 'Fetch Master Data' in dashboard")
        needs_action = True
    
    if len(products) != len(db_products):
        logger.info("\n📋 ACTION NEEDED: Fetch Products")
        logger.info("   Run: python fetch_products.py")
        logger.info("   Or click 'Fetch Master Data' in dashboard")
        needs_action = True
    
    if len(dcs) != len(db_dcs):
        logger.info("\n📋 ACTION NEEDED: Fetch DCs")
        logger.info("   Run: python fetch_invoices.py")
        logger.info("   Or click 'Fetch Invoices' in dashboard")
        needs_action = True
    
    if not needs_action:
        logger.info("\n✓ Everything is in sync!")
        logger.info("  All data from Tally is in the database")
    
    logger.info("\n" + "=" * 80)
    logger.info("TEST COMPLETE")
    logger.info("=" * 80)

if __name__ == "__main__":
    test_complete_flow()
