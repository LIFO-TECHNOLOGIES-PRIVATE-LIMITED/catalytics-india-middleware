"""
Auto-Fetch Implementation for Missing Products & Customers

Add this code to fetch_invoices.py to enable quick fetching of missing data.
"""

import requests
import logging
from typing import List, Tuple
import os

logger = logging.getLogger("tally_invoice_fetcher")

# ============================================================================
# CONFIGURATION
# ============================================================================

# Read from environment
AUTO_FETCH_PRODUCTS = os.getenv('AUTO_FETCH_PRODUCTS', 'true').lower() == 'true'
AUTO_FETCH_CUSTOMERS = os.getenv('AUTO_FETCH_CUSTOMERS', 'true').lower() == 'true'
CATALYTICS_API_BASE_URL = os.getenv('CATALYTICS_API_BASE_URL', 'http://localhost:8000/')
ENTITY_ID = int(os.getenv('ENTITY_ID', '29'))
TALLY_URL = os.getenv('TALLY_URL', 'http://localhost:9000/')
TALLY_COMPANY = os.getenv('TALLY_COMPANY', 'CHENNAI OXYGEN')

API_TIMEOUT_SECONDS = 30
API_RETRY_COUNT = 1

logger.info(f"[AUTO-FETCH CONFIG] AUTO_FETCH_PRODUCTS={AUTO_FETCH_PRODUCTS}")
logger.info(f"[AUTO-FETCH CONFIG] AUTO_FETCH_CUSTOMERS={AUTO_FETCH_CUSTOMERS}")
logger.info(f"[AUTO-FETCH CONFIG] API_URL={CATALYTICS_API_BASE_URL}")


# ============================================================================
# MAIN FUNCTION
# ============================================================================

def fetch_missing_data_from_backend(
    missing_products: List[str],
    missing_customers: List[str]
) -> Tuple[bool, dict]:
    """
    Call backend API to fetch missing products and customers from Tally.
    
    Args:
        missing_products: List of product names to fetch
        missing_customers: List of customer names to fetch
    
    Returns:
        Tuple of (success: bool, result: dict)
        - success: True if fetch was successful or skipped
        - result: Response data from API
    """
    
    # Skip if both are empty
    if not missing_products and not missing_customers:
        return True, {"status": "skipped", "reason": "No missing data"}
    
    # Skip if auto-fetch is disabled
    if not AUTO_FETCH_PRODUCTS and not AUTO_FETCH_CUSTOMERS:
        logger.info(f"[AUTO-FETCH] Auto-fetch is disabled - skipping")
        return True, {"status": "skipped", "reason": "Auto-fetch disabled"}
    
    try:
        api_url = f"{CATALYTICS_API_BASE_URL.rstrip('/')}/import/fetch-missing-data/"
        
        # Filter based on configuration
        products_to_fetch = missing_products if AUTO_FETCH_PRODUCTS else []
        customers_to_fetch = missing_customers if AUTO_FETCH_CUSTOMERS else []
        
        payload = {
            "entity_id": ENTITY_ID,
            "tally_url": TALLY_URL,
            "company": TALLY_COMPANY,
            "missing_products": products_to_fetch,
            "missing_customers": customers_to_fetch
        }
        
        logger.info(f"[AUTO-FETCH] Calling backend API to fetch missing data...")
        logger.info(f"[AUTO-FETCH] Products: {len(products_to_fetch)}, Customers: {len(customers_to_fetch)}")
        
        response = requests.post(api_url, json=payload, timeout=API_TIMEOUT_SECONDS)
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"[AUTO-FETCH] ✓ Success: {result.get('message', 'Data fetched')}")
            
            results = result.get('results', {})
            logger.info(
                f"[AUTO-FETCH] Products: {results.get('products_fetched', 0)} fetched, "
                f"{results.get('products_failed', 0)} failed, "
                f"{results.get('products_skipped', 0)} skipped"
            )
            logger.info(
                f"[AUTO-FETCH] Customers: {results.get('customers_fetched', 0)} fetched, "
                f"{results.get('customers_failed', 0)} failed, "
                f"{results.get('customers_skipped', 0)} skipped"
            )
            
            return True, result
        else:
            logger.warning(f"[AUTO-FETCH] ✗ API returned status {response.status_code}")
            logger.warning(f"[AUTO-FETCH] Response: {response.text[:500]}")
            return False, {"status": "error", "code": response.status_code}
            
    except requests.Timeout:
        logger.error(f"[AUTO-FETCH] ✗ API request timeout ({API_TIMEOUT_SECONDS}s)")
        return False, {"status": "error", "reason": "timeout"}
    except requests.ConnectionError as e:
        logger.error(f"[AUTO-FETCH] ✗ Connection error: {e}")
        return False, {"status": "error", "reason": "connection_error"}
    except Exception as e:
        logger.error(f"[AUTO-FETCH] ✗ Unexpected error: {e}", exc_info=True)
        return False, {"status": "error", "reason": str(e)}


# ============================================================================
# INTEGRATION POINTS
# ============================================================================

"""
WHERE TO ADD THIS IN fetch_invoices.py:

1. At the top of the file, add the import:
   from AUTO_FETCH_IMPLEMENTATION import fetch_missing_data_from_backend

2. In the run_once() function, collect missing data:

   # Collect missing products and customers
   missing_products = set()
   missing_customers = set()
   
   for dc in delivery_notes:
       # ... existing code to check customer ...
       if not customer_exists:
           missing_customers.add(customer_name)
           continue
       
       # ... existing code to check products ...
       for item in items:
           if not product_exists:
               missing_products.add(item_name)
       
       if missing_products:
           continue

3. After collecting all missing data, try to fetch them:

   # Try to fetch missing data
   if missing_products or missing_customers:
       logger.info(f"[DC FETCH] Found missing data - attempting auto-fetch...")
       logger.info(f"[DC FETCH] Missing: {len(missing_products)} products, {len(missing_customers)} customers")
       
       success, result = fetch_missing_data_from_backend(
           list(missing_products),
           list(missing_customers)
       )
       
       if success:
           logger.info(f"[DC FETCH] Missing data fetched successfully")
           # Optionally: Retry DC fetch here
       else:
           logger.warning(f"[DC FETCH] Failed to fetch missing data - will retry in next cycle")

4. Return statistics including auto-fetch results:

   return {
       'total_fetched': total_fetched,
       'new_saved': new_saved,
       'updated_saved': updated_saved,
       'already_exists': already_exists,
       'skipped_missing_customer': skipped_missing_customer,
       'skipped_missing_product': skipped_missing_product,
       'auto_fetch_attempted': auto_fetch_attempted,
       'auto_fetch_successful': auto_fetch_successful,
       'errors': errors,
   }
"""


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    # Example: Fetch missing products and customers
    missing_products = ["OXYGEN 50 KG", "NITROGEN 40 LIT"]
    missing_customers = ["Customer A", "Customer B"]
    
    success, result = fetch_missing_data_from_backend(missing_products, missing_customers)
    
    if success:
        print(f"✓ Auto-fetch successful")
        print(f"Result: {result}")
    else:
        print(f"✗ Auto-fetch failed")
        print(f"Error: {result}")
