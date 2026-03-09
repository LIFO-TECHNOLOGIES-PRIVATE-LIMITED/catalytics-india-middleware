"""
Simple test for DC fetch from http://192.168.1.44:9000/
Just tests the Tally API call without database
"""
import logging
import sys
import os

ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import tally_client

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_dc_simple():
    """Simple test of DC fetch from Tally"""
    tally_url = "http://192.168.1.44:9000/"
    company_name = "Chennai Oxygen"
    
    logger.info("=" * 80)
    logger.info("Testing DC fetch from Tally (API only)")
    logger.info("URL: %s", tally_url)
    logger.info("Company: %s", company_name)
    logger.info("=" * 80)
    
    try:
        # Fetch DCs using the updated get_delivery_notes function
        logger.info("\nFetching DCs...")
        dcs = tally_client.get_delivery_notes(
            company_name=company_name,
            url=tally_url,
            from_date="20200101",
            to_date="20991231"
        )
        
        logger.info("\n✓ Successfully fetched %d DCs", len(dcs))
        
        if dcs:
            logger.info("\nDC Details:")
            for i, dc in enumerate(dcs, 1):
                logger.info("\n  DC #%d:", i)
                logger.info("    Voucher Number: %s", dc.get("VOUCHERNUMBER", "N/A"))
                logger.info("    Date: %s", dc.get("DATE", "N/A"))
                logger.info("    Party: %s", dc.get("PARTYLEDGERNAME", "N/A"))
                logger.info("    Reference: %s", dc.get("REFERENCE", "N/A"))
                logger.info("    Vehicle No: %s", dc.get("BASICSHIPPEDBY", "N/A"))
                logger.info("    Destination: %s", dc.get("BASICFINALDESTINATION", "N/A"))
                
                inventory = dc.get("INVENTORY", [])
                logger.info("    Items: %d", len(inventory))
                
                if inventory:
                    logger.info("    Inventory:")
                    for item in inventory:
                        logger.info("      - %s: %s %s @ %s", 
                                   item.get("STOCKITEMNAME", "N/A"),
                                   item.get("ACTUALQTY", "N/A"),
                                   item.get("BILLEDQTY", "N/A"),
                                   item.get("RATE", "N/A"))
                
                # Show consignee info if available
                consignee = dc.get("CONSIGNEE", {})
                if consignee:
                    logger.info("    Consignee:")
                    logger.info("      Name: %s", consignee.get("NAME", "N/A"))
                    logger.info("      Address: %s", consignee.get("ADDRESS", "N/A"))
                    logger.info("      State: %s", consignee.get("STATE", "N/A"))
                    logger.info("      GSTIN: %s", consignee.get("GSTIN", "N/A"))
        else:
            logger.warning("No DCs found!")
        
        logger.info("\n=== Test Complete ===")
        return True
        
    except Exception as e:
        logger.error("DC fetch failed: %s", e, exc_info=True)
        return False

if __name__ == "__main__":
    success = test_dc_simple()
    sys.exit(0 if success else 1)
