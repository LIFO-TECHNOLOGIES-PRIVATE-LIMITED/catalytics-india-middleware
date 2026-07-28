"""
Fetch products (stock items) from all active Tally companies.
Parses stock item names to extract product master, unit, variant, and product type.

Tally stock item name format:
    INDUSTRIAL OXYGEN 4 CUM (CYL)
    └─ product ─────┘ │ └┘  └─┘
                      qty unit  type_code

Stock items are normally classified by a recognized type code in parentheses.
Type codes configured via PRODUCT_TYPE_MAP in .env:
    CYL:CYLINDER, PLT:PALLET, TNK:TANK, CON:CONTAINER

Special rules:
- products whose names start with LPG are always fetched with product type LPG,
  even if they do not use a configured suffix.
- products whose names mention Vaparizer are always fetched with product type
  VAPARIZER and default to variant/unit 1 Nos when no size is present.
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
from mapping_lookup import get_mapping_lookup
from sync_catalytics import create_auto_ticket

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

# Fallback regex: <PRODUCT NAME> (<TYPE_CODE>) — no quantity/unit
STOCK_NAME_PATTERN_NO_VARIANT = re.compile(r'^(.+?)\s+\((\w+)\)$')

# Default variant when product name has type code but no quantity/unit
DEFAULT_VARIANT = '7 Cum'
DEFAULT_UNIT = 'Cum'
LPG_TYPE_CODE = 'LPG'
VAPARIZER_TYPE_CODE = 'VAPARIZER'
VAPARIZER_DEFAULT_VARIANT = '1 Nos'
VAPARIZER_DEFAULT_UNIT = 'Nos'
LPG_NAME_PATTERN = re.compile(
    r'^(?P<base>LPG(?:\s+[A-Za-z]+)*)(?:\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]+))?(?:\s+\((?P<legacy_type>\w+)\))?$',
    re.IGNORECASE,
)
VAPARIZER_NAME_PATTERN = re.compile(
    r'^(?P<base>.*?\bVA?POU?RIZER\b.*?)(?:\s+(?P<qty>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]+))?$',
    re.IGNORECASE,
)
# Also match names like "Monthly Rental (Vaporizer)" where the keyword is in parentheses
VAPARIZER_PAREN_PATTERN = re.compile(
    r'^(?P<base>.+?)\s*\(\s*(?:VA?POU?RIZER)\s*\)\s*$',
    re.IGNORECASE,
)


def _is_vaparizer_name(name: str) -> bool:
    """Check if a name contains any spelling of vaporizer/vaparizer."""
    upper = name.upper()
    return 'VAPARIZER' in upper or 'VAPORIZER' in upper or 'VAPOURIZER' in upper


def _parse_vaparizer_stock_item_name(normalized_name: str):
    if not _is_vaparizer_name(normalized_name):
        return None

    # Try parenthesized form first: "Monthly Rental (Vaporizer)"
    paren_match = VAPARIZER_PAREN_PATTERN.match(normalized_name)
    if paren_match:
        product_master_name = paren_match.group('base').strip()
        variant_name = VAPARIZER_DEFAULT_VARIANT
        unit_name = VAPARIZER_DEFAULT_UNIT
        product_type_name = config.get_product_type_name(VAPARIZER_TYPE_CODE) or 'Vaparizer'
        return {
            'product_master_name': product_master_name,
            'unit_name': unit_name,
            'variant_name': variant_name,
            'product_type_code': VAPARIZER_TYPE_CODE,
            'product_type_name': product_type_name,
            'canonical_name': f"{product_master_name} {variant_name} ({VAPARIZER_TYPE_CODE})",
            'variant_defaulted': True,
        }

    # Standard form: "VAPARIZER 1 Nos" or just "VAPARIZER"
    match = VAPARIZER_NAME_PATTERN.match(normalized_name)
    if not match:
        return None

    product_master_name = match.group('base').strip()
    quantity = (match.group('qty') or '1').strip()
    unit_name = (match.group('unit') or VAPARIZER_DEFAULT_UNIT).strip()
    variant_name = f"{quantity} {unit_name}".strip()
    product_type_name = config.get_product_type_name(VAPARIZER_TYPE_CODE) or 'Vaparizer'

    return {
        'product_master_name': product_master_name,
        'unit_name': unit_name,
        'variant_name': variant_name,
        'product_type_code': VAPARIZER_TYPE_CODE,
        'product_type_name': product_type_name,
        'canonical_name': f"{product_master_name} {variant_name} ({VAPARIZER_TYPE_CODE})",
        'variant_defaulted': match.group('qty') is None,
    }


def _parse_lpg_stock_item_name(normalized_name: str):
    if not normalized_name.upper().startswith(LPG_TYPE_CODE):
        return None

    match = LPG_NAME_PATTERN.match(normalized_name)
    if not match:
        return None

    product_master_name = match.group('base').strip()
    quantity = (match.group('qty') or '').strip()
    unit_name = (match.group('unit') or '').strip()
    variant_name = f"{quantity} {unit_name}".strip()
    product_type_name = config.get_product_type_name(LPG_TYPE_CODE) or LPG_TYPE_CODE

    if variant_name:
        canonical_name = f"{product_master_name} {variant_name} ({LPG_TYPE_CODE})"
    else:
        canonical_name = f"{product_master_name} ({LPG_TYPE_CODE})"

    return {
        'product_master_name': product_master_name,
        'unit_name': unit_name,
        'variant_name': variant_name,
        'product_type_code': LPG_TYPE_CODE,
        'product_type_name': product_type_name,
        'canonical_name': canonical_name,
    }


def parse_stock_item_name(name: str):
    """
    Parse a Tally stock item name into product components.

    Primary pattern:   <NAME> <QTY> <UNIT> (<TYPE_CODE>)  e.g. "Oxygen Gas 7 Cum (CYL)"
    Fallback pattern:  <NAME> (<TYPE_CODE>)                e.g. "Oxygen Gas (CYL)"
      -> fallback uses default variant '7 Cum'
    LPG override:        Any name starting with LPG is accepted and forced to
                         product type LPG, with qty/unit parsed from the tail when present.
    Vaparizer override: Any name mentioning Vaparizer is accepted and forced to
                         product type VAPARIZER, defaulting to variant/unit 1 Nos.

    Returns dict with product_master_name, variant_name, unit_name,
    product_type_code, product_type_name, canonical_name
    - or None if unrecognized.
    """
    if not name:
        return None

    # Normalize: collapse multiple spaces into single space
    normalized_name = ' '.join(name.strip().split())
    type_map = config.PRODUCT_TYPE_MAP

    # Special product rules use dedicated overrides instead of PRODUCT_TYPE_MAP suffixes.
    parsed_vaparizer = _parse_vaparizer_stock_item_name(normalized_name)
    if parsed_vaparizer:
        return parsed_vaparizer

    parsed_lpg = _parse_lpg_stock_item_name(normalized_name)
    if parsed_lpg:
        return parsed_lpg

    # --- Primary pattern: name includes quantity and unit ---
    match = STOCK_NAME_PATTERN.match(normalized_name)
    if match:
        product_master_name = match.group(1).strip()
        quantity = match.group(2).strip()
        unit_name = match.group(3).strip()
        type_code = match.group(4).strip().upper()

        if type_code not in type_map:
            return None

        canonical_variant = f"{quantity} {unit_name}"
        canonical_name = f"{product_master_name} {canonical_variant} ({type_code})"

        return {
            'product_master_name': product_master_name,
            'unit_name': unit_name,
            'variant_name': canonical_variant,
            'product_type_code': type_code,
            'product_type_name': type_map[type_code],
            'canonical_name': canonical_name,
        }

    # --- Fallback pattern: name ends with (TYPE_CODE) but has no quantity/unit ---
    match_no_var = STOCK_NAME_PATTERN_NO_VARIANT.match(normalized_name)
    if match_no_var:
        product_master_name = match_no_var.group(1).strip()
        type_code = match_no_var.group(2).strip().upper()

        if type_code not in type_map:
            return None

        canonical_name = f"{product_master_name} {DEFAULT_VARIANT} ({type_code})"

        return {
            'product_master_name': product_master_name,
            'unit_name': DEFAULT_UNIT,
            'variant_name': DEFAULT_VARIANT,
            'product_type_code': type_code,
            'product_type_name': type_map[type_code],
            'canonical_name': canonical_name,
            'variant_defaulted': True,  # flag so caller can log it
        }

    return None


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
    logger.info(f"Product type rules: {config.get_product_type_map()}")

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

                # Try mapping.json lookup first (from Discovery Agent)
                mapping_entry = get_mapping_lookup().classify(
                    product_name,
                    tally_guid=(item.get('GUID') or item.get('MASTERID') or '').strip(),
                )
                if mapping_entry:
                    parsed = {
                        'product_master_name': mapping_entry.get('product_master_name', product_name),
                        'variant_name': mapping_entry.get('variant_master_name', '') or mapping_entry.get('variant_name', ''),
                        'unit_name': mapping_entry.get('unit_master_name', 'numbers'),
                        'product_type_code': mapping_entry.get('product_type_code', ''),
                        'product_type_name': mapping_entry.get('product_type_name', ''),
                        'canonical_name': product_name,
                        'from_mapping': True,
                    }
                    logger.info(
                        f"[MAPPING LOOKUP] '{product_name}' -> "
                        f"product={parsed['product_master_name']}, "
                        f"type={parsed['product_type_name']}, "
                        f"variant={parsed['variant_name']}"
                    )
                else:
                    parsed = parse_stock_item_name(product_name)

                if not parsed:
                    # Check if the name matches any PRODUCT_EXACT_KEYWORDS
                    name_lower = product_name.lower()
                    matched_keyword = next(
                        (kw for kw in config.PRODUCT_EXACT_KEYWORDS if kw in name_lower),
                        None
                    )
                    if matched_keyword:
                        parsed = {
                            'product_master_name': product_name,
                            'variant_name': '',
                            'unit_name': '',
                            'product_type_code': '',
                            'product_type_name': '',
                            'canonical_name': product_name,
                        }
                        logger.info(
                            f"[EXACT KEYWORD MATCH] '{product_name}' matched keyword '{matched_keyword}' — saved as-is"
                        )
                    else:
                        logger.debug(
                            f"[SKIPPED] '{product_name}' — no matching type code "
                            f"(expected one of: {list(config.get_product_type_codes())})"
                        )
                        overall_stats['skipped_no_type'] += 1
                        continue

                overall_stats['matched'] += 1
                canonical_name = parsed['canonical_name']
                tally_guid = (item.get('GUID') or item.get('MASTERID') or item.get('REMOTEID') or '').strip()
                variant_note = " [DEFAULT VARIANT 7 Cum]" if parsed.get('variant_defaulted') else ""
                logger.info(
                    f"[PARSED{variant_note}] '{product_name}' -> "
                    f"product={parsed['product_master_name']}, "
                    f"variant={parsed['variant_name']}, "
                    f"unit={parsed['unit_name']}, "
                    f"type={parsed['product_type_code']}({parsed['product_type_name']}), "
                    f"GUID={tally_guid or 'N/A'}, "
                    f"canonical='{canonical_name}'"
                )

                # --- Name-based lookup (product name is the unique key) ---
                existing_by_name = db.product_exists(product_name)
                if not existing_by_name:
                    existing_by_name = db.product_exists_normalized(canonical_name)

                if existing_by_name:
                    owner_company = existing_by_name['tally_company']
                    if owner_company == company_name:
                        try:
                            product_data = _map_stock_item_to_product(item, company_name, parsed)
                            product_data['name_canonical'] = canonical_name
                            db.update_product(existing_by_name['id'], product_data)
                            logger.info(
                                f"[UPDATED] '{product_name}' "
                                f"(company: {company_name}, GUID: {tally_guid or 'N/A'}, "
                                f"HSN: {product_data.get('hsn_code', 'N/A')})"
                            )
                            overall_stats['updated'] += 1
                        except Exception as e:
                            logger.error(f"[ERROR] Failed to update product '{product_name}': {e}", exc_info=True)
                            overall_stats['errors'] += 1
                    else:
                        logger.warning(
                            f"[DUPLICATE SKIPPED] '{product_name}' "
                            f"(canonical: '{canonical_name}', "
                            f"owned by {owner_company}, attempted by {company_name})"
                        )
                        db.log_duplicate(
                            entity_type='product',
                            entity_name=canonical_name,
                            tally_company=company_name,
                            owned_by_company=owner_company,
                            details=f"GUID: {tally_guid or 'N/A'}, HSN: {item.get('HSNCODE', 'N/A')}",
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
            import traceback as _tb
            create_auto_ticket(
                api_base_url=config.CATALYTICS_API_BASE,
                subject='Product Fetch: company fetch failed',
                description=(
                    f'Failed to fetch products for company "{company_name}".\n\n'
                    + _tb.format_exc()
                ),
                entity_id=config.ENTITY_ID,
                priority=2,
                category=11,
                error_code='PRODUCT_FETCH_COMPANY_ERROR',
            )

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
        import traceback as _tb
        create_auto_ticket(
            api_base_url=config.CATALYTICS_API_BASE,
            subject='Product Fetch: unhandled exception during fetch run',
            description=_tb.format_exc(),
            entity_id=config.ENTITY_ID,
            priority=1,
            category=11,
            error_code='PRODUCT_FETCH_CRASH',
        )
        exit(1)
