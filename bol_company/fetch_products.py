"""
Fetch products from multiple Tally companies
Parses stock item names to extract product master, unit, variant, and product type.

Product type detection (by keyword in name):
    - Name contains "pallet"  → PALLET  (variant: 105, unit: cubic)
    - Name contains "liquid"  → TANK    (variant: 230, unit: litter)
    - Default                  → CYLINDER (variant: 7,   unit: cubic)

All stock items are accepted regardless of name format.
"""
import re
import json
import logging
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


def _attach_product_fetch_file_handler():
    """Ensure product fetch logs also go to a dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'product_fetch.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)

    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'product_fetch_file':
            return

    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    file_handler.name = 'product_fetch_file'
    logger.addHandler(file_handler)


_attach_product_fetch_file_handler()

# BOL product type rules (checked in order against lower-case name)
# Detection is keyword-based and does not rely on PRODUCT_TYPE_MAP for classification.
#   "pallet" → PALLET  | variant=105, unit=cubic
#   "liquid" → TANK    | variant=200, unit=liter
#   default  → CYLINDER | variant=7,   unit=cubic
_BOL_TYPE_RULES = [
    ('pallet', 'PLT', 'PALLET', '105', 'cubic'),
    ('liquid', 'TNK', 'TANK',   '200', 'liter'),
]

_UNIT_ABBREVIATIONS = {
    'cubic': 'cum',
    'liter': 'ltr',
    'litre': 'ltr',
}

def canonical_unit_name(unit):
    """Return a canonical abbreviation for a given unit string."""
    if not unit:
        return ''
    normalized = str(unit).strip().lower()
    return _UNIT_ABBREVIATIONS.get(normalized, normalized)



def parse_stock_item_name(name):
    """
    Parse a Tally stock item name into product components.

    BOL keyword-based product type detection (checked in order):
      - Name contains 'pallet' → PALLET  | variant=105, unit=cubic
      - Name contains 'liquid' → TANK    | variant=200, unit=liter
      - Default               → CYLINDER | variant=7,   unit=cubic

    Unit names are stored as canonical abbreviations ('cum', 'ltr').
    All stock item names are accepted (no strict format required).
    The full stock item name is used as the product_master_name.

    Args:
        name: e.g. "LIQUID OXYGEN GAS" or "Nitrogen Gas" or "PALLET 30 NO - 7 CM"

    Returns:
        dict with keys: product_master_name, unit_name, variant_name,
                        product_type_code, product_type_name, canonical_name
    """
    if not name:
        return None

    # Normalize: collapse multiple spaces into single space
    normalized_name = ' '.join(name.strip().split())
    name_lower = normalized_name.lower()

    # Determine product type, variant, and unit by keyword detection
    type_code = 'CYL'
    type_name = 'CYLINDER'
    canonical_variant = '7'
    unit_name = canonical_unit_name('cubic')

    for keyword, t_code, t_name, variant, unit in _BOL_TYPE_RULES:
        if keyword in name_lower:
            type_code = t_code
            type_name = t_name
            canonical_variant = variant
            unit_name = canonical_unit_name(unit)
            break

    product_master_name = normalized_name

    # Canonical name for duplicate checking: "PRODUCT_MASTER (TYPE_CODE)"
    canonical_name = f"{product_master_name} ({type_code})"

    return {
        'product_master_name': product_master_name,
        'unit_name': unit_name,
        'variant_name': canonical_variant,
        'product_type_code': type_code,
        'product_type_name': type_name,
        'canonical_name': canonical_name,
    }


def fetch_products_from_all_companies():
    """
    Fetch products from all active Tally companies.
    Determines product type by keyword detection in the stock item name:
      - 'pallet' → PALLET  (variant=105, unit=cubic)
      - 'liquid' → TANK    (variant=200, unit=liter)
      - default  → CYLINDER (variant=7, unit=cubic)
    All stock items are accepted; no format restriction.
    First-come-first-served duplicate prevention.
    """
    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        from sync_to_catalytics import create_auto_ticket as _ticket
        _ticket(
            api_base_url=config.CATALYTICS_API_BASE,
            subject='Product Fetch: no active Tally companies configured',
            description='No active Tally companies are configured. Check TALLY_COMPANIES in .env.',
            entity_id=config.ENTITY_ID,
            priority=1,
            category=11,
            error_code='PRODUCT_FETCH_NO_COMPANIES',
        )
        return {
            'total_fetched': 0,
            'matched': 0,
            'new_saved': 0,
            'duplicates_skipped': 0,
            'skipped_no_type': 0,
            'errors': 0,
        }

    logger.info(f"Starting product fetch from {len(active_companies)} companies")
    logger.info(f"Active companies: {', '.join(active_companies)}")
    logger.info(f"Product type map: {config.PRODUCT_TYPE_MAP}")

    overall_stats = {
        'total_fetched': 0,
        'matched': 0,
        'new_saved': 0,
        'duplicates_skipped': 0,
        'skipped_no_type': 0,
        'errors': 0
    }

    for company_name in active_companies:
        company_key = config.get_company_key(company_name)
        logger.info(f"\n{'='*60}")
        logger.info(f"Processing: {company_name} ({company_key})")
        logger.info(f"{'='*60}")

        try:
            # Step 1: Fetch all stock items from Tally
            products = tally.get_products(company_name)
            overall_stats['total_fetched'] += len(products)

            if not products:
                logger.warning(f"No products fetched from {company_name}")
                continue

            logger.info(f"Fetched {len(products)} stock items from Tally")

            # Step 2: Filter and parse each stock item
            for product in products:
                product_name = product['name']

                # Use GUID as fallback if name is empty
                if not product_name:
                    product_name = product.get('guid', '')
                    if product_name:
                        logger.warning(f"Product name is empty, using GUID as name: {product_name}")
                    else:
                        logger.warning("Skipping product with empty name and no GUID")
                        continue

                # Parse stock item name
                parsed = parse_stock_item_name(product_name)

                if not parsed:
                    # parse_stock_item_name only returns None for empty name — skip
                    logger.debug(f"[SKIPPED] '{product_name}' — empty name")
                    overall_stats['skipped_no_type'] += 1
                    continue

                overall_stats['matched'] += 1
                canonical_name = parsed['canonical_name']
                tally_guid = product.get('guid', '')

                logger.info(
                    f"[PARSED] '{product_name}' -> "
                    f"product={parsed['product_master_name']}, "
                    f"variant={parsed['variant_name']}, "
                    f"unit={parsed['unit_name']}, "
                    f"type={parsed['product_type_code']}({parsed['product_type_name']}), "
                    f"HSN={product.get('hsn_code', '')}, "
                    f"GST={product.get('gst_rate', 0)}%"
                )

                # Build product data dict (shared for insert and update)
                product_data = {
                    'tally_guid': tally_guid,
                    'name': product_name,
                    'name_canonical': canonical_name,
                    'tally_company': company_name,
                    'hsn_code': product.get('hsn_code', ''),
                    'unit': product.get('unit', ''),
                    'rate': product.get('rate', 0.0),
                    'description': product.get('description', ''),
                    'data_json': json.dumps(product),
                    'product_master_name': parsed['product_master_name'],
                    'variant_name': parsed['variant_name'],
                    'unit_name': parsed['unit_name'],
                    'product_type_code': parsed['product_type_code'],
                    'product_type_name': parsed['product_type_name'],
                    'gst_applicable': product.get('gst_applicable', ''),
                    'gst_rate': product.get('gst_rate', 0.0),
                    'igst_rate': product.get('igst_rate', 0.0),
                    'cgst_rate': product.get('cgst_rate', 0.0),
                    'sgst_rate': product.get('sgst_rate', 0.0),
                }

                # Step 3: GUID-based lookup — update existing or create new
                try:
                    existing = db.product_exists_by_guid(tally_guid) if tally_guid else None

                    if existing:
                        # GUID found — update with latest data from Tally
                        db.update_product(existing['id'], product_data)
                        logger.info(
                            f"[UPDATED] '{product_name}' (GUID: {tally_guid}, "
                            f"SQLite ID: {existing['id']})"
                        )
                        overall_stats['duplicates_skipped'] += 1
                    else:
                        # New GUID — create new record
                        db.insert_product(product_data)
                        logger.info(
                            f"[NEW PRODUCT] '{product_name}' saved "
                            f"(company: {company_name}, GUID: {tally_guid}, "
                            f"type={parsed['product_type_name']})"
                        )
                        overall_stats['new_saved'] += 1

                except Exception as e:
                    logger.error(
                        f"[ERROR] Failed to save product '{product_name}': {e}",
                        exc_info=True
                    )
                    overall_stats['errors'] += 1

        except Exception as e:
            logger.error(
                f"[ERROR] Failed to process company '{company_name}': {e}",
                exc_info=True
            )
            overall_stats['errors'] += 1
            import traceback as _tb_fp
            from sync_to_catalytics import create_auto_ticket as _ticket
            _ticket(
                api_base_url=config.CATALYTICS_API_BASE,
                subject='Product Fetch: company fetch failed',
                description=f'Failed to fetch for company "{company_name}".\n\n' + _tb_fp.format_exc(),
                entity_id=config.ENTITY_ID,
                priority=2,
                category=11,
                error_code='PRODUCT_FETCH_COMPANY_ERROR',
            )

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info("PRODUCT FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Stock Items from Tally: {overall_stats['total_fetched']}")
    logger.info(f"Matched (with type code):     {overall_stats['matched']}")
    logger.info(f"Skipped (no type code):        {overall_stats['skipped_no_type']}")
    logger.info(f"New Products Created:          {overall_stats['new_saved']}")
    logger.info(f"Existing Products Updated:     {overall_stats['duplicates_skipped']}")
    logger.info(f"Errors:                        {overall_stats['errors']}")
    logger.info(f"{'='*60}")

    db_stats = db.get_statistics()
    logger.info(f"\nDatabase Statistics:")
    logger.info(f"Total Products: {db_stats['total_products']}")
    logger.info(f"Products by Company: {db_stats['products_by_company']}")
    logger.info(f"Synced Products: {db_stats['synced_products']}")

    db.close()
    return overall_stats


if __name__ == '__main__':
    try:
        logger.info("="*60)
        logger.info("BOL - PRODUCT FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        stats = fetch_products_from_all_companies()

        logger.info("\n✓ Product fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Product fetch failed: {e}", exc_info=True)
        import traceback as _tb_fp2
        from sync_to_catalytics import create_auto_ticket as _ticket
        _ticket(
            api_base_url=config.CATALYTICS_API_BASE,
            subject='Product Fetch: unhandled exception during fetch run',
            description=_tb_fp2.format_exc(),
            entity_id=config.ENTITY_ID,
            priority=1,
            category=11,
            error_code='PRODUCT_FETCH_CRASH',
        )
        exit(1)
