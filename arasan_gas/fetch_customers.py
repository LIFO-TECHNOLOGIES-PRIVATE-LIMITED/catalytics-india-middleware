"""
Fetch customers from multiple Tally companies (all ledger groups).
GUID-based create/update logic — existing customers are updated, new ones created.
Non-customer ledgers (sales/expense/tax) are detected and skipped automatically.
"""
import json
import logging
import re
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


# ---------------------------------------------------------------------------
# Non-customer ledger detection
# ---------------------------------------------------------------------------

# Known Tally top-level groups that are never customers
_NON_CUSTOMER_GROUPS = frozenset({
    'sales accounts',
    'purchase accounts',
    'direct expenses',
    'indirect expenses',
    'direct incomes',
    'indirect incomes',
    'duties & taxes',
    'duties and taxes',
    'bank accounts',
    'bank od a/c',
    'cash-in-hand',
    'fixed assets',
    'current liabilities',
    'investments',
    'capital account',
    'reserves & surplus',
    'loans (liability)',
    'loans & advances (asset)',
    'provisions',
    'stock-in-hand',
    'misc. expenses (asset)',
    'branch / divisions',
    'suspense a/c',
    'primary',
})

# Keywords in custom sub-group names that indicate non-customer ledgers
_NON_CUSTOMER_GROUP_KEYWORDS = (
    'sales account',
    'purchase account',
    'expense',
    'income a/c',
    'duties & tax',
    'duties and tax',
    'tax payable',
    'tax receivable',
)

# Ledger names ending with "@ XX%" are GST-rate sales/purchase ledgers, not customers
_GST_RATE_PATTERN = re.compile(r'@\s*\d+(\.\d+)?\s*%\s*$')


def _is_non_customer_ledger(name: str, parent_group: str) -> bool:
    """Return True if this ledger is clearly NOT a customer (sales/expense/tax ledger)."""
    pg = (parent_group or '').strip().lower()

    if pg in _NON_CUSTOMER_GROUPS:
        return True

    for kw in _NON_CUSTOMER_GROUP_KEYWORDS:
        if kw in pg:
            return True

    # "PRODUCT NAME @ 18%" — GST-rate suffix means it's a sales/purchase ledger
    if _GST_RATE_PATTERN.search(name or ''):
        return True

    return False


def _normalize_customer_name(raw_name):
    return ' '.join((raw_name or '').split())


def _customer_changed(existing_row, customer_data):
    """Return True only when meaningful customer fields changed."""
    fields = (
        'name', 'tally_company', 'gstin', 'pan', 'address',
        'state', 'city', 'pincode', 'phone', 'email', 'data_json'
    )
    for f in fields:
        old_val = existing_row[f] if existing_row and f in existing_row.keys() else None
        new_val = customer_data.get(f)
        if (old_val or '') != (new_val or ''):
            return True
    return False


def fetch_customers_from_all_companies():
    """
    Fetch ALL customers (all ledger groups) from all active Tally companies.
    GUID-based create/update — existing customers updated, new ones created.
    Non-customer ledgers (sales/expense/tax) are detected and skipped.
    """
    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        return {
            'total_fetched': 0,
            'new_saved': 0,
            'updated': 0,
            'skipped_non_customer': 0,
            'errors': 0,
        }

    logger.info(f"Starting customer fetch from {len(active_companies)} companies")
    logger.info(f"Active companies: {', '.join(active_companies)}")

    overall_stats = {
        'total_fetched': 0,
        'new_saved': 0,
        'updated': 0,
        'skipped_non_customer': 0,
        'errors': 0,
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

            logger.info(f"Fetched {len(customers)} ledgers from Tally")

            for customer in customers:
                customer_name = _normalize_customer_name(customer.get('name'))
                customer['name'] = customer_name
                tally_guid = customer.get('guid', '')
                parent_group = customer.get('parent_group', '')

                if not customer_name:
                    logger.warning("Skipping ledger with empty name")
                    continue

                # Skip non-customer ledgers (sales, expense, tax, GST-rate suffix)
                if _is_non_customer_ledger(customer_name, parent_group):
                    logger.info(
                        f"[SKIPPED] '{customer_name}' (group: '{parent_group}') "
                        f"— non-customer ledger"
                    )
                    overall_stats['skipped_non_customer'] += 1
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
                        if _customer_changed(existing, customer_data):
                            db.update_customer(existing['id'], customer_data)
                            logger.info(
                                f"[UPDATED] '{customer_name}' "
                                f"(GUID: {tally_guid}, ID: {existing['id']})"
                            )
                            overall_stats['updated'] += 1
                        else:
                            logger.debug(f"[UNCHANGED] '{customer_name}'")
                    else:
                        db.insert_customer(customer_data)
                        logger.info(
                            f"[NEW] '{customer_name}' "
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

    logger.info(f"\n{'='*60}")
    logger.info("CUSTOMER FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Fetched from Tally:     {overall_stats['total_fetched']}")
    logger.info(f"Non-customer ledgers skipped: {overall_stats['skipped_non_customer']}")
    logger.info(f"New Customers Created:        {overall_stats['new_saved']}")
    logger.info(f"Existing Customers Updated:   {overall_stats['updated']}")
    logger.info(f"Errors:                       {overall_stats['errors']}")
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
        logger.info("ARASAN GAS - CUSTOMER FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        stats = fetch_customers_from_all_companies()

        logger.info("\n✓ Customer fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Customer fetch failed: {e}", exc_info=True)
        exit(1)
