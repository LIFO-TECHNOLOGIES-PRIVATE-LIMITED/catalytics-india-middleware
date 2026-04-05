"""
Fetch all customers (all ledger groups) from all active Tally companies.
Implements duplicate prevention based on customer name (first-come-first-served).
"""
import json
import logging
import os
from pathlib import Path
from datetime import datetime

import sys
ROOT_DIR = os.path.abspath(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from config import config, BASE_DIR
from db import Database
import tally_api

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def _attach_file_handler():
    log_path = Path(str(BASE_DIR)) / 'logs' / 'customer_fetch.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'customer_fetch_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'customer_fetch_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_file_handler()


def _normalize_customer_name(raw_name: str) -> str:
    """Normalize customer name: collapse internal whitespace (same as arasan)."""
    return ' '.join((raw_name or '').split())


def _map_ledger_to_customer(ledger: dict, company_name: str) -> dict:
    """Map raw Tally ledger dict (UPPERCASE keys) to customer data dict."""
    name = _normalize_customer_name(ledger.get('NAME') or ledger.get('LEDGERNAME') or '')
    guid = (ledger.get('GUID') or ledger.get('MASTERID') or ledger.get('REMOTEID') or '').strip()

    # GST: parse_ledgers_full already normalises to GSTIN
    gstin = (ledger.get('GSTIN') or ledger.get('GSTREGISTRATIONNUMBER') or
             ledger.get('PARTYGSTIN') or ledger.get('GSTREGISTRATION') or '').strip()
    if gstin.startswith(':'):
        gstin = gstin.lstrip(':')

    pan = (ledger.get('PAN') or ledger.get('INCOMETAXNUMBER') or
           ledger.get('PANCARDNUMBER') or ledger.get('PANNUMBER') or '').strip()

    # State / pincode: parse_ledgers_full normalises to STATE and PINCODE
    state = (ledger.get('STATE') or ledger.get('STATENAME') or
             ledger.get('PRIORSTATENAME') or ledger.get('LEDSTATENAME') or '').strip()
    pincode = (ledger.get('PINCODE') or ledger.get('PINCODENO') or
               ledger.get('LEDGERPINCODE') or '').strip()

    # Phone: parse_ledgers_full normalises to MOBILE
    phone = (ledger.get('MOBILE') or ledger.get('LEDGERMOBILE') or
             ledger.get('LEDPHONE') or ledger.get('PHONE') or
             ledger.get('MOBILENO') or ledger.get('PHONENUMBER') or '').strip()

    email = (ledger.get('EMAIL') or ledger.get('LEDGEREMAIL') or
             ledger.get('EMAILID') or '').strip()

    # Primary address: parse_ledgers_full sets PRIMARY_ADDRESS from LEDMAILINGDETAILS.LIST
    address = (ledger.get('PRIMARY_ADDRESS') or '').strip()
    if not address:
        addresses = ledger.get('ADDRESSES')
        if isinstance(addresses, list) and addresses:
            address = ', '.join(str(a) for a in addresses if a)
        else:
            address = (ledger.get('MAILINGNAME') or '').strip()

    # Delivery addresses: list of dicts with name/address/state/pincode/gstin
    delivery_addresses = ledger.get('DELIVERY_ADDRESSES') or []

    return {
        'tally_guid': guid,
        'name': name,
        'tally_company': company_name,
        'gstin': gstin,
        'pan': pan,
        'address': address,
        'state': state,
        'city': '',
        'pincode': pincode,
        'phone': phone,
        'email': email,
        'delivery_addresses_json': json.dumps(delivery_addresses) if delivery_addresses else None,
        'data_json': json.dumps(ledger),
    }


def fetch_customers_from_all_companies():
    """
    Fetch all customers (all ledger groups) from all active Tally companies.
    First-come-first-served duplicate prevention by customer name.
    """
    db = Database(config.SQLITE_DB_PATH)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        return {'total_fetched': 0, 'new_saved': 0, 'duplicates_skipped': 0, 'errors': 0}

    logger.info(f"Starting customer fetch from {len(active_companies)} companies")
    logger.info(f"Active companies: {', '.join(active_companies)}")

    overall_stats = {'total_fetched': 0, 'new_saved': 0, 'updated': 0, 'duplicates_skipped': 0, 'errors': 0}

    for company_name in active_companies:
        company_key = config.get_company_key(company_name)
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {company_name} ({company_key})")
        logger.info(f"{'='*60}")

        try:
            ledgers = tally_api.get_ledgers(company_name, config.TALLY_URL)
            overall_stats['total_fetched'] += len(ledgers)

            if not ledgers:
                logger.warning(f"No customers fetched from {company_name}")
                continue

            logger.info(f"Fetched {len(ledgers)} customers from Tally")

            for ledger in ledgers:
                customer_name = _normalize_customer_name(ledger.get('NAME') or ledger.get('LEDGERNAME') or '')

                if not customer_name:
                    logger.warning("Skipping customer with empty name")
                    continue

                guid = (ledger.get('GUID') or ledger.get('MASTERID') or ledger.get('REMOTEID') or '').strip()

                # --- Name-based lookup (primary unique key) ---
                existing = db.customer_exists(customer_name)
                # GUID lookup only if name didn't match
                if not existing and guid:
                    existing = db.customer_exists_by_guid(guid)

                if existing:
                    owner_company = existing['tally_company']
                    if owner_company == company_name:
                        try:
                            customer_data = _map_ledger_to_customer(ledger, company_name)
                            db.update_customer(existing['id'], customer_data)
                            da_count = len(customer_data.get('delivery_addresses_json') and __import__('json').loads(customer_data['delivery_addresses_json']) or [])
                            logger.info(
                                f"[UPDATED] '{customer_name}' "
                                f"(company: {company_name}, GUID: {guid or 'N/A'}, "
                                f"GSTIN: {customer_data.get('gstin') or 'N/A'}, "
                                f"phone: {customer_data.get('phone') or 'N/A'}, "
                                f"email: {customer_data.get('email') or 'N/A'}, "
                                f"state: {customer_data.get('state') or 'N/A'}, "
                                f"pincode: {customer_data.get('pincode') or 'N/A'}, "
                                f"delivery_addresses: {da_count})"
                            )
                            overall_stats['updated'] += 1
                        except Exception as e:
                            logger.error(f"[ERROR] Failed to update customer '{customer_name}': {e}", exc_info=True)
                            overall_stats['errors'] += 1
                    else:
                        logger.warning(
                            f"[DUPLICATE SKIPPED] '{customer_name}' "
                            f"(owned by {owner_company}, attempted by {company_name})"
                        )
                        db.log_duplicate(
                            entity_type='customer',
                            entity_name=customer_name,
                            tally_company=company_name,
                            owned_by_company=owner_company,
                            details=f"GUID: {guid or 'N/A'}, GSTIN: {ledger.get('GSTREGISTRATIONNUMBER', 'N/A')}",
                        )
                        overall_stats['duplicates_skipped'] += 1
                    continue

                try:
                    customer_data = _map_ledger_to_customer(ledger, company_name)
                    db.insert_customer(customer_data)
                    da_count = len(customer_data.get('delivery_addresses_json') and __import__('json').loads(customer_data['delivery_addresses_json']) or [])
                    logger.info(
                        f"[NEW CUSTOMER] '{customer_name}' saved "
                        f"(company: {company_name}, GUID: {customer_data.get('tally_guid') or 'N/A'}, "
                        f"GSTIN: {customer_data.get('gstin') or 'N/A'}, "
                        f"phone: {customer_data.get('phone') or 'N/A'}, "
                        f"email: {customer_data.get('email') or 'N/A'}, "
                        f"state: {customer_data.get('state') or 'N/A'}, "
                        f"pincode: {customer_data.get('pincode') or 'N/A'}, "
                        f"delivery_addresses: {da_count})"
                    )
                    overall_stats['new_saved'] += 1

                except Exception as e:
                    logger.error(f"[ERROR] Failed to save customer '{customer_name}': {e}", exc_info=True)
                    overall_stats['errors'] += 1

        except Exception as e:
            logger.error(f"[ERROR] Failed to process company '{company_name}': {e}", exc_info=True)
            overall_stats['errors'] += 1

    logger.info(f"\n{'='*60}")
    logger.info("CUSTOMER FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Fetched from Tally:  {overall_stats['total_fetched']}")
    logger.info(f"New Customers Saved:        {overall_stats['new_saved']}")
    logger.info(f"Updated (same company):     {overall_stats['updated']}")
    logger.info(f"Duplicates Skipped:         {overall_stats['duplicates_skipped']}")
    logger.info(f"Errors:                     {overall_stats['errors']}")
    logger.info(f"{'='*60}")

    db_stats = db.get_statistics()
    logger.info(f"Total Customers in DB: {db_stats['total_customers']}")
    logger.info(f"Synced Customers:      {db_stats['synced_customers']}")
    logger.info(f"Customers by Company:  {db_stats['customers_by_company']}")

    db.close()
    return overall_stats


if __name__ == '__main__':
    try:
        logger.info("=" * 60)
        logger.info("CHENNAI OXYGEN - CUSTOMER FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        stats = fetch_customers_from_all_companies()
        logger.info("\n✓ Customer fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Customer fetch failed: {e}", exc_info=True)
        exit(1)
