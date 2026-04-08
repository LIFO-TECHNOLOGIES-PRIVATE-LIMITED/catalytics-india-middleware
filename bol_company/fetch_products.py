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

# BOL product type rules (checked in order against lower-case name):
#   "pallet" → PALLET  | variant=105, unit=cubic
#   "liquid" → TANK    | variant=230, unit=litter
#   default  → CYLINDER | variant=7,   unit=cubic
_BOL_TYPE_RULES = [
    ('pallet', 'PLT', 'PALLET', '105', 'cubic'),
    ('liquid', 'TNK', 'TANK',   '230', 'litter'),
]


def parse_stock_item_name(name):
    """
    Parse a Tally stock item name into product components.

    BOL keyword-based product type detection (checked in order):
      - Name contains 'pallet' → PALLET  | variant=105, unit=cubic
      - Name contains 'liquid' → TANK    | variant=230, unit=litter
      - Default               → CYLINDER | variant=7,   unit=cubic

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
    unit_name = 'cubic'

    for keyword, t_code, t_name, variant, unit in _BOL_TYPE_RULES:
        if keyword in name_lower:
            type_code = t_code
            type_name = t_name
            canonical_variant = variant
            unit_name = unit
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
      - 'liquid' → TANK    (variant=230, unit=litter)
      - default  → CYLINDER (variant=7, unit=cubic)
    All stock items are accepted; no format restriction.
    First-come-first-served duplicate prevention.
    """
    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
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

                if not product_name:
                    logger.warning("Skipping product with empty name")
                    continue

                # Parse stock item name
                parsed = parse_stock_item_name(product_name)

                if not parsed:
                    # parse_stock_item_name only returns None for empty name — skip
                    logger.debug(f"[SKIPPED] '{product_name}' — empty name")
                    overall_stats['skipped_no_type'] += 1
                    continue

                overall_stats['matched'] += 1
                logger.info(
                    f"[PARSED] '{product_name}' -> "
                    f"product={parsed['product_master_name']}, "
                    f"variant={parsed['variant_name']}, "
                    f"unit={parsed['unit_name']}, "
                    f"type={parsed['product_type_code']}({parsed['product_type_name']}), "
                    f"HSN={product.get('hsn_code', '')}, "
                    f"GST={product.get('gst_rate', 0)}%"
                )

                # Step 3: Check duplicate using canonical name
                # Canonical name normalizes spacing in quantity/unit
                canonical_name = parsed['canonical_name']
                
                existing = db.product_exists_normalized(canonical_name)

                if existing:
                    owner_company = existing['tally_company']
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
                        details=f"HSN: {product.get('hsn_code', 'N/A')}, Original: {product_name}"
                    )
                    overall_stats['duplicates_skipped'] += 1
                    continue

                # Step 4: Save to SQLite with parsed fields
                try:
                    product_data = {
                        'tally_guid': product['guid'],
                        'name': product_name,
                        'name_canonical': canonical_name,  # ← Canonical name for uniqueness
                        'tally_company': company_name,
                        'hsn_code': product.get('hsn_code', ''),
                        'unit': product.get('unit', ''),
                        'rate': product.get('rate', 0.0),
                        'description': product.get('description', ''),
                        'data_json': json.dumps(product),
                        # Parsed fields
                        'product_master_name': parsed['product_master_name'],
                        'variant_name': parsed['variant_name'],
                        'unit_name': parsed['unit_name'],
                        'product_type_code': parsed['product_type_code'],
                        'product_type_name': parsed['product_type_name'],
                        # GST fields from Tally
                        'gst_applicable': product.get('gst_applicable', ''),
                        'gst_rate': product.get('gst_rate', 0.0),
                        'igst_rate': product.get('igst_rate', 0.0),
                        'cgst_rate': product.get('cgst_rate', 0.0),
                        'sgst_rate': product.get('sgst_rate', 0.0),
                    }

                    db.insert_product(product_data)

                    logger.info(
                        f"[NEW PRODUCT] '{product_name}' saved "
                        f"(company: {company_name}, "
                        f"product={parsed['product_master_name']}, "
                        f"variant={parsed['variant_name']}, "
                        f"type={parsed['product_type_name']}, "
                        f"HSN={product.get('hsn_code', '')}, "
                        f"GST={product.get('gst_rate', 0)}%)"
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

    # Summary
    logger.info(f"\n{'='*60}")
    logger.info("PRODUCT FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Stock Items from Tally: {overall_stats['total_fetched']}")
    logger.info(f"Matched (with type code):     {overall_stats['matched']}")
    logger.info(f"Skipped (no type code):        {overall_stats['skipped_no_type']}")
    logger.info(f"New Products Saved:            {overall_stats['new_saved']}")
    logger.info(f"Duplicates Skipped:            {overall_stats['duplicates_skipped']}")
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
        exit(1)
