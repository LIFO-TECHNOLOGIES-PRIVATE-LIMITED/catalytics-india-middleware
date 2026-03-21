"""
Fetch products (stock items) from all active Tally companies.
Parses stock item names to extract product master, unit, variant, and product type.

Tally stock item name format:
    INDUSTRIAL OXYGEN 4 CUM (CYL)
    └─ product ─────┘ │ └┘  └─┘
                      qty unit  type_code

Only stock items with a recognized type code in parentheses are saved.
Type codes configured via PRODUCT_TYPE_MAP in .env:
    CYL:CYLINDER, PLT:PALLET, TNK:TANK, CON:CONTAINER
"""
import re
import json
import logging
from pathlib import Path
from datetime import datetime

import os
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
    log_path = Path(str(BASE_DIR)) / 'logs' / 'product_fetch.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'product_fetch_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'product_fetch_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_file_handler()

# Regex: <PRODUCT NAME> <QTY> <UNIT> (<TYPE_CODE>)
# Handles spacing variations: "1.5CUM" and "1.5 CUM" both match
STOCK_NAME_PATTERN = re.compile(r'^(.+?)\s+(\d+(?:\.\d+)?)\s*(\w+)\s+\((\w+)\)$')


def parse_stock_item_name(name: str):
    """
    Parse a Tally stock item name into product components.
    Handles spacing variations in quantity/unit (e.g., "1.5CUM" vs "1.5 CUM").

    Returns dict with product_master_name, variant_name, unit_name,
    product_type_code, product_type_name, canonical_name
    — or None if unrecognized.
    """
    if not name:
        return None

    # Normalize: collapse multiple spaces into single space
    normalized_name = ' '.join(name.strip().split())

    match = STOCK_NAME_PATTERN.match(normalized_name)
    if not match:
        return None

    product_master_name = match.group(1).strip()
    quantity = match.group(2).strip()
    unit_name = match.group(3).strip()
    type_code = match.group(4).strip().upper()

    type_map = config.PRODUCT_TYPE_MAP
    if type_code not in type_map:
        return None

    # Canonical variant always has space between quantity and unit
    canonical_variant = f"{quantity} {unit_name}"

    # Canonical name for uniqueness — ensures "1.5CUM" and "1.5 CUM" are the same product
    canonical_name = f"{product_master_name} {canonical_variant} ({type_code})"

    return {
        'product_master_name': product_master_name,
        'unit_name': unit_name,
        'variant_name': canonical_variant,
        'product_type_code': type_code,
        'product_type_name': type_map[type_code],
        'canonical_name': canonical_name,
    }


def _safe_float(value) -> float:
    try:
        return float(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return 0.0


def _map_stock_item_to_product(item: dict, company_name: str, parsed: dict) -> dict:
    """Map raw Tally stock item dict (UPPERCASE keys) to product data dict."""
    return {
        'tally_guid': (item.get('GUID') or item.get('MASTERID') or item.get('REMOTEID') or '').strip(),
        'name': (item.get('NAME') or '').strip(),
        'tally_company': company_name,
        'hsn_code': (item.get('HSNCODE') or '').strip(),
        'unit': (item.get('BASEUNITS') or item.get('UOM') or '').strip(),
        'rate': _safe_float(item.get('STDCOST') or item.get('LASTPURCHASEPRICE') or 0),
        'description': (item.get('DESCRIPTION') or '').strip(),
        'data_json': json.dumps(item),
        # Parsed name fields
        'product_master_name': parsed['product_master_name'],
        'variant_name': parsed['variant_name'],
        'unit_name': parsed['unit_name'],
        'product_type_code': parsed['product_type_code'],
        'product_type_name': parsed['product_type_name'],
        # GST fields
        'gst_applicable': (item.get('GSTAPPLICABLE') or item.get('GSTAPPLICABILITY') or '').strip(),
        'gst_rate': _safe_float(item.get('GST_RATE') or item.get('GSTRATIOOFDUTY') or 0),
        'igst_rate': _safe_float(item.get('IGST_RATE') or item.get('IGSTRATIO') or 0),
        'cgst_rate': _safe_float(item.get('CGST_RATE') or item.get('CGSTTAXRATE') or 0),
        'sgst_rate': _safe_float(item.get('SGST_RATE') or item.get('SGSTTAXRATE') or 0),
    }


def fetch_products_from_all_companies():
    """
    Fetch products from all active Tally companies.
    Only saves stock items with recognized type codes.
    First-come-first-served duplicate prevention.
    """
    db = Database(config.SQLITE_DB_PATH)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        return {
            'total_fetched': 0, 'matched': 0, 'new_saved': 0,
            'duplicates_skipped': 0, 'skipped_no_type': 0, 'errors': 0,
        }

    logger.info(f"Starting product fetch from {len(active_companies)} companies")
    logger.info(f"Active companies: {', '.join(active_companies)}")
    logger.info(f"Product type map: {config.PRODUCT_TYPE_MAP}")

    overall_stats = {
        'total_fetched': 0, 'matched': 0, 'new_saved': 0, 'updated': 0,
        'duplicates_skipped': 0, 'skipped_no_type': 0, 'errors': 0,
    }

    for company_name in active_companies:
        company_key = config.get_company_key(company_name)
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {company_name} ({company_key})")
        logger.info(f"{'='*60}")

        try:
            stock_items = tally_api.get_stock_items(company_name, config.TALLY_URL)
            overall_stats['total_fetched'] += len(stock_items)

            if not stock_items:
                logger.warning(f"No stock items fetched from {company_name}")
                continue

            logger.info(f"Fetched {len(stock_items)} stock items from Tally")

            for item in stock_items:
                product_name = (item.get('NAME') or '').strip()

                if not product_name:
                    logger.warning("Skipping product with empty name")
                    continue

                parsed = parse_stock_item_name(product_name)

                if not parsed:
                    logger.debug(
                        f"[SKIPPED] '{product_name}' — no matching type code "
                        f"(expected one of: {list(config.PRODUCT_TYPE_MAP.keys())})"
                    )
                    overall_stats['skipped_no_type'] += 1
                    continue

                overall_stats['matched'] += 1
                canonical_name = parsed['canonical_name']
                tally_guid = (item.get('GUID') or item.get('MASTERID') or item.get('REMOTEID') or '').strip()
                logger.info(
                    f"[PARSED] '{product_name}' -> "
                    f"product={parsed['product_master_name']}, "
                    f"variant={parsed['variant_name']}, "
                    f"unit={parsed['unit_name']}, "
                    f"type={parsed['product_type_code']}({parsed['product_type_name']}), "
                    f"GUID={tally_guid or 'N/A'}, "
                    f"canonical='{canonical_name}'"
                )

                # --- GUID-based lookup (takes priority over canonical name) ---
                existing_by_guid = db.product_exists_by_guid(tally_guid) if tally_guid else None
                if existing_by_guid:
                    owner_company = existing_by_guid['tally_company']
                    if owner_company == company_name:
                        try:
                            product_data = _map_stock_item_to_product(item, company_name, parsed)
                            product_data['name_canonical'] = canonical_name
                            db.update_product(existing_by_guid['id'], product_data)
                            logger.info(
                                f"[UPDATED by GUID] '{product_name}' "
                                f"(company: {company_name}, GUID: {tally_guid}, "
                                f"HSN: {product_data.get('hsn_code', 'N/A')})"
                            )
                            overall_stats['updated'] += 1
                        except Exception as e:
                            logger.error(f"[ERROR] Failed to update product '{product_name}' by GUID: {e}", exc_info=True)
                            overall_stats['errors'] += 1
                    else:
                        logger.warning(
                            f"[DUPLICATE SKIPPED by GUID] '{product_name}' "
                            f"(GUID: {tally_guid}, owned by {owner_company}, attempted by {company_name})"
                        )
                        db.log_duplicate(
                            entity_type='product',
                            entity_name=canonical_name,
                            tally_company=company_name,
                            owned_by_company=owner_company,
                            details=f"GUID: {tally_guid}, HSN: {item.get('HSNCODE', 'N/A')}",
                        )
                        overall_stats['duplicates_skipped'] += 1
                    continue

                # --- Canonical name-based lookup (fallback) ---
                existing = db.product_exists_normalized(canonical_name)

                if existing:
                    owner_company = existing['tally_company']
                    if owner_company == company_name:
                        # Same company — update with fresh Tally data
                        try:
                            product_data = _map_stock_item_to_product(item, company_name, parsed)
                            product_data['name_canonical'] = canonical_name
                            db.update_product(existing['id'], product_data)
                            logger.info(
                                f"[UPDATED by name] '{product_name}' "
                                f"(company: {company_name}, "
                                f"GUID: {product_data.get('tally_guid', 'N/A')}, "
                                f"HSN: {product_data.get('hsn_code', 'N/A')})"
                            )
                            overall_stats['updated'] += 1
                        except Exception as e:
                            logger.error(f"[ERROR] Failed to update product '{product_name}': {e}", exc_info=True)
                            overall_stats['errors'] += 1
                    else:
                        # Different company — cross-company duplicate, skip
                        logger.warning(
                            f"[DUPLICATE SKIPPED] '{product_name}' "
                            f"(canonical: '{canonical_name}') "
                            f"(owned by {owner_company}, attempted by {company_name})"
                        )
                        db.log_duplicate(
                            entity_type='product',
                            entity_name=canonical_name,
                            tally_company=company_name,
                            owned_by_company=owner_company,
                            details=f"HSN: {item.get('HSNCODE', 'N/A')}",
                        )
                        overall_stats['duplicates_skipped'] += 1
                    continue

                try:
                    product_data = _map_stock_item_to_product(item, company_name, parsed)
                    product_data['name_canonical'] = canonical_name
                    db.insert_product(product_data)
                    logger.info(
                        f"[NEW PRODUCT] '{product_name}' saved "
                        f"(company: {company_name}, "
                        f"product={parsed['product_master_name']}, "
                        f"variant={parsed['variant_name']}, "
                        f"type={parsed['product_type_name']}, "
                        f"GUID={product_data.get('tally_guid', 'N/A')}, "
                        f"HSN={item.get('HSNCODE', '')})"
                    )
                    overall_stats['new_saved'] += 1

                except Exception as e:
                    logger.error(f"[ERROR] Failed to save product '{product_name}': {e}", exc_info=True)
                    overall_stats['errors'] += 1

        except Exception as e:
            logger.error(f"[ERROR] Failed to process company '{company_name}': {e}", exc_info=True)
            overall_stats['errors'] += 1

    logger.info(f"\n{'='*60}")
    logger.info("PRODUCT FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Stock Items from Tally: {overall_stats['total_fetched']}")
    logger.info(f"Matched (with type code):     {overall_stats['matched']}")
    logger.info(f"Skipped (no type code):        {overall_stats['skipped_no_type']}")
    logger.info(f"New Products Saved:            {overall_stats['new_saved']}")
    logger.info(f"Updated (same company):        {overall_stats['updated']}")
    logger.info(f"Duplicates Skipped:            {overall_stats['duplicates_skipped']}")
    logger.info(f"Errors:                        {overall_stats['errors']}")
    logger.info(f"{'='*60}")

    db_stats = db.get_statistics()
    logger.info(f"Total Products in DB: {db_stats['total_products']}")
    logger.info(f"Synced Products:      {db_stats['synced_products']}")
    logger.info(f"Products by Company:  {db_stats['products_by_company']}")

    db.close()
    return overall_stats


if __name__ == '__main__':
    try:
        logger.info("=" * 60)
        logger.info("CHENNAI OXYGEN - PRODUCT FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)

        stats = fetch_products_from_all_companies()
        logger.info("\n✓ Product fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Product fetch failed: {e}", exc_info=True)
        exit(1)
