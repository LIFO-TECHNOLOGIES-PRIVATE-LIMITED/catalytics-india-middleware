"""
Fetch master data (customers + products) from all active Tally companies.
Called by automation_manager.py on a scheduled interval.
"""
import logging
from datetime import datetime
from fetch_customers import fetch_customers_from_all_companies
from fetch_products import fetch_products_from_all_companies

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

if __name__ == '__main__':
    try:
        logger.info("=" * 60)
        logger.info("BOL - MASTER DATA FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        logger.info("Fetching customers...")
        customer_stats = fetch_customers_from_all_companies()

        logger.info("Fetching products...")
        product_stats = fetch_products_from_all_companies()

        logger.info("=" * 60)
        logger.info("MASTER DATA FETCH COMPLETE")
        logger.info(f"Customers - New: {customer_stats.get('new_saved', 0)}, "
                    f"Duplicates: {customer_stats.get('duplicates_skipped', 0)}, "
                    f"Errors: {customer_stats.get('errors', 0)}")
        logger.info(f"Products  - New: {product_stats.get('new_saved', 0)}, "
                    f"Duplicates: {product_stats.get('duplicates_skipped', 0)}, "
                    f"Errors: {product_stats.get('errors', 0)}")
        logger.info("=" * 60)

    except Exception as e:
        logger.error(f"Master data fetch failed: {e}", exc_info=True)
        exit(1)
