"""
Quick verification script to run after restarting dashboard
Checks if everything is working correctly
"""
import logging
import sys
import os
import sqlite3
import time

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import config as cfg

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def verify():
    """Verify everything is working"""
    
    # Load config
    cfg.load_env_file(cfg.resolve_env_path(ROOT_DIR))
    db_path = cfg.get_env("TALLY_DB_PATH")
    
    logger.info("=" * 80)
    logger.info("VERIFICATION AFTER RESTART")
    logger.info("=" * 80)
    logger.info("Database: %s", db_path)
    logger.info("")
    
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # Check customers
        customers = conn.execute("SELECT COUNT(*) as count FROM ledgers").fetchone()["count"]
        logger.info("✓ Customers in database: %d", customers)
        if customers >= 3:
            logger.info("  ✓ GOOD - Expected 3 customers")
        else:
            logger.warning("  ⚠ Expected 3 customers, found %d", customers)
        
        # Check products
        products = conn.execute("SELECT COUNT(*) as count FROM stock_items").fetchone()["count"]
        logger.info("✓ Products in database: %d", products)
        if products >= 6:
            logger.info("  ✓ GOOD - Expected 6 products")
        else:
            logger.warning("  ⚠ Expected 6 products, found %d", products)
        
        # Check DCs
        dcs = conn.execute("SELECT COUNT(*) as count FROM delivery_notes").fetchone()["count"]
        logger.info("✓ DCs in database: %d", dcs)
        if dcs >= 1:
            logger.info("  ✓ GOOD - Expected 1 DC")
            
            # Show DC details
            dc = conn.execute("""
                SELECT dc_no, voucher_date, party_ledger_name 
                FROM delivery_notes 
                LIMIT 1
            """).fetchone()
            logger.info("  DC Details:")
            logger.info("    - DC No: %s", dc["dc_no"])
            logger.info("    - Date: %s", dc["voucher_date"])
            logger.info("    - Party: %s", dc["party_ledger_name"])
        else:
            logger.warning("  ⚠ Expected 1 DC, found %d", dcs)
            logger.warning("  → Wait for automation to run (30 seconds)")
            logger.warning("  → Or click 'Fetch Invoices' in dashboard")
        
        conn.close()
        
        logger.info("")
        logger.info("=" * 80)
        logger.info("VERIFICATION COMPLETE")
        logger.info("=" * 80)
        
        if customers >= 3 and products >= 6 and dcs >= 1:
            logger.info("✓ ALL CHECKS PASSED!")
            logger.info("  Everything is working correctly")
            return True
        else:
            logger.warning("⚠ SOME CHECKS FAILED")
            logger.warning("  Wait a few minutes for automation to run")
            logger.warning("  Or manually trigger fetch from dashboard")
            return False
        
    except Exception as e:
        logger.error("✗ Verification failed: %s", e)
        return False

if __name__ == "__main__":
    success = verify()
    sys.exit(0 if success else 1)
