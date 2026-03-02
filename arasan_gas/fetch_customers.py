"""
Fetch customers from multiple Tally companies
Implements duplicate prevention based on customer name
"""
import json
import logging
from datetime import datetime
from config import config
from db import Database
from tally_client import TallyClient

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def fetch_customers_from_all_companies():
    """
    Fetch customers from all active Tally companies
    First-come-first-served duplicate prevention
    """
    # Initialize
    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        return {
            'total_fetched': 0,
            'new_saved': 0,
            'duplicates_skipped': 0,
            'errors': 0,
        }

    logger.info(f"Starting customer fetch from {len(active_companies)} companies")
    logger.info(f"Active companies: {', '.join(active_companies)}")

    # Overall statistics
    overall_stats = {
        'total_fetched': 0,
        'new_saved': 0,
        'duplicates_skipped': 0,
        'errors': 0
    }

    # Process each company in order (priority by configuration order)
    for company_name in active_companies:
        company_key = config.get_company_key(company_name)
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {company_name} ({company_key})")
        logger.info(f"{'='*60}")

        try:
            # Step 1: Fetch customers from Tally
            customers = tally.get_customers(company_name)
            overall_stats['total_fetched'] += len(customers)

            if not customers:
                logger.warning(f"No customers fetched from {company_name}")
                continue

            logger.info(f"Fetched {len(customers)} customers from Tally")

            # Step 2: Process each customer with duplicate check
            for customer in customers:
                customer_name = customer['name']

                if not customer_name:
                    logger.warning(f"Skipping customer with empty name")
                    continue

                # Step 3: Check if customer name already exists
                existing = db.customer_exists(customer_name)

                if existing:
                    # Duplicate found - skip and log
                    owner_company = existing['tally_company']
                    logger.warning(
                        f"[DUPLICATE SKIPPED] '{customer_name}' "
                        f"(owned by {owner_company}, attempted by {company_name})"
                    )

                    # Log duplicate for audit
                    db.log_duplicate(
                        entity_type='customer',
                        entity_name=customer_name,
                        tally_company=company_name,
                        owned_by_company=owner_company,
                        details=f"GSTIN: {customer.get('gstin', 'N/A')}"
                    )

                    overall_stats['duplicates_skipped'] += 1
                    continue

                # Step 4: New customer - save to SQLite
                try:
                    customer_data = {
                        'tally_guid': customer['guid'],
                        'name': customer_name,
                        'tally_company': company_name,
                        'gstin': customer.get('gstin', ''),
                        'pan': customer.get('pan', ''),
                        'address': customer.get('address', ''),
                        'state': customer.get('state', ''),
                        'city': customer.get('city', ''),
                        'pincode': customer.get('pincode', ''),
                        'phone': customer.get('phone', ''),
                        'email': customer.get('email', ''),
                        'data_json': json.dumps(customer)  # Store full Tally response
                    }

                    db.insert_customer(customer_data)

                    logger.info(
                        f"[NEW CUSTOMER] '{customer_name}' saved "
                        f"(company: {company_name}, GSTIN: {customer.get('gstin', 'N/A')})"
                    )
                    overall_stats['new_saved'] += 1

                except Exception as e:
                    logger.error(
                        f"[ERROR] Failed to save customer '{customer_name}': {e}",
                        exc_info=True
                    )
                    overall_stats['errors'] += 1

        except Exception as e:
            logger.error(
                f"[ERROR] Failed to process company '{company_name}': {e}",
                exc_info=True
            )
            overall_stats['errors'] += 1

    # Step 5: Print summary
    logger.info(f"\n{'='*60}")
    logger.info("CUSTOMER FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Fetched from Tally: {overall_stats['total_fetched']}")
    logger.info(f"New Customers Saved: {overall_stats['new_saved']}")
    logger.info(f"Duplicates Skipped: {overall_stats['duplicates_skipped']}")
    logger.info(f"Errors: {overall_stats['errors']}")
    logger.info(f"{'='*60}")

    # Step 6: Update database statistics
    db_stats = db.get_statistics()
    logger.info(f"\nDatabase Statistics:")
    logger.info(f"Total Customers: {db_stats['total_customers']}")
    logger.info(f"Customers by Company: {db_stats['customers_by_company']}")
    logger.info(f"Synced Customers: {db_stats['synced_customers']}")

    db.close()
    return overall_stats


if __name__ == '__main__':
    try:
        logger.info("="*60)
        logger.info("ARASAN GAS - CUSTOMER FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        stats = fetch_customers_from_all_companies()

        logger.info("\n✓ Customer fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Customer fetch failed: {e}", exc_info=True)
        exit(1)
