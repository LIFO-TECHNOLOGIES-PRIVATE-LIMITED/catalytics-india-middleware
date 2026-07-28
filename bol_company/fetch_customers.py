"""
Fetch customers from multiple Tally companies (all ledger groups).
GUID-based create/update logic — existing customers are updated, new ones created.
"""
import json
import logging
import os
from pathlib import Path
from datetime import datetime
from config import config, BASE_DIR
from db import Database
from tally_client import TallyClient

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def _attach_customer_fetch_file_handler():
    """Ensure customer fetch logs also go to a dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'customer_fetch.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'customer_fetch_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'customer_fetch_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_customer_fetch_file_handler()

def _normalize_customer_name(raw_name):
    return ' '.join((raw_name or '').split())

def fetch_customers_from_all_companies():
    """
    Fetch ALL customers (all ledger groups) from all active Tally companies.
    GUID-based create/update — existing customers updated, new ones created.
    """
    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        from sync_to_catalytics import create_auto_ticket as _ticket
        _ticket(
            api_base_url=config.CATALYTICS_API_BASE,
            subject='Customer Fetch: no active Tally companies configured',
            description='No active Tally companies are configured. Check TALLY_COMPANIES in .env.',
            entity_id=config.ENTITY_ID,
            priority=1,
            category=11,
            error_code='CUSTOMER_FETCH_NO_COMPANIES',
        )
        return {
            'total_fetched': 0,
            'new_saved': 0,
            'updated': 0,
            'errors': 0,
        }

    logger.info(f"Starting customer fetch from {len(active_companies)} companies")
    logger.info(f"Active companies: {', '.join(active_companies)}")

    overall_stats = {
        'total_fetched': 0,
        'new_saved': 0,
        'updated': 0,
        'errors': 0
    }

    for company_name in active_companies:
        company_key = config.get_company_key(company_name)
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {company_name} ({company_key})")
        logger.info(f"{'='*60}")

        try:
            customers = tally.get_customers(company_name)
            overall_stats['total_fetched'] += len(customers)

            if not customers:
                logger.warning(f"No customers fetched from {company_name}")
                continue

            logger.info(f"Fetched {len(customers)} customers from Tally")

            for customer in customers:
                customer_name = _normalize_customer_name(customer.get('name'))
                customer['name'] = customer_name
                tally_guid = customer.get('guid', '')

                if not customer_name:
                    logger.warning("Skipping customer with empty name")
                    continue

                customer_data = {
                    'tally_guid': tally_guid,
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
                    'data_json': json.dumps(customer),
                }

                try:
                    existing = db.customer_exists_by_guid(tally_guid) if tally_guid else None

                    if existing:
                        db.update_customer(existing['id'], customer_data)
                        logger.info(
                            f"[UPDATED] '{customer_name}' (GUID: {tally_guid}, "
                            f"SQLite ID: {existing['id']})"
                        )
                        overall_stats['updated'] += 1
                    else:
                        db.insert_customer(customer_data)
                        logger.info(
                            f"[NEW CUSTOMER] '{customer_name}' saved "
                            f"(company: {company_name}, GUID: {tally_guid})"
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
            import traceback as _tb_fc
            from sync_to_catalytics import create_auto_ticket as _ticket
            _ticket(
                api_base_url=config.CATALYTICS_API_BASE,
                subject='Customer Fetch: company fetch failed',
                description=f'Failed to fetch for company "{company_name}".\n\n' + _tb_fc.format_exc(),
                entity_id=config.ENTITY_ID,
                priority=2,
                category=11,
                error_code='CUSTOMER_FETCH_COMPANY_ERROR',
            )

    logger.info(f"\n{'='*60}")
    logger.info("CUSTOMER FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Fetched from Tally:   {overall_stats['total_fetched']}")
    logger.info(f"New Customers Created:      {overall_stats['new_saved']}")
    logger.info(f"Existing Customers Updated: {overall_stats['updated']}")
    logger.info(f"Errors:                     {overall_stats['errors']}")
    logger.info(f"{'='*60}")

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
        logger.info("BOL - CUSTOMER FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        stats = fetch_customers_from_all_companies()

        logger.info("\n✓ Customer fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Customer fetch failed: {e}", exc_info=True)
        import traceback as _tb_fc2
        from sync_to_catalytics import create_auto_ticket as _ticket
        _ticket(
            api_base_url=config.CATALYTICS_API_BASE,
            subject='Customer Fetch: unhandled exception during fetch run',
            description=_tb_fc2.format_exc(),
            entity_id=config.ENTITY_ID,
            priority=1,
            category=11,
            error_code='CUSTOMER_FETCH_CRASH',
        )
        exit(1)
