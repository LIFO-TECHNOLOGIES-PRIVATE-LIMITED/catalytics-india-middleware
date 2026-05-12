"""
Fetch products from multiple Tally companies.

Each stock item generates one DB row PER variant for its type:
    CO2      → 30kg  (CYL)                          — 1 variant
    LIQUID O2→ 200/230/247/250 lit (TNK)             — 4 variants
    LIQUID N2→ 200/250 litre (TNK)                   — 2 variants
    PALLET   → 105cum (PLT)                          — 1 variant
    CYLINDER → 7cum / 10cum (CYL)   [default]        — 2 variants

Name format: "{clean_base} {size}{unit} ({type_code})"
  e.g.  "ARGON BHOX 7cum (CYL)", "ARGON BHOX 10cum (CYL)"
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

# ---------------------------------------------------------------------------
# Variant definitions — mirrors fetch_customers.py ledger-product variants
# Each entry: (size_str, unit_abbr, type_code, type_name)
# ---------------------------------------------------------------------------
_PRODUCT_VARIANTS = {
    'CO2': [
        ('30',  'kg',    'CYL', 'CYLINDER'),
    ],
    'LIQUID_O2': [
        ('200', 'lit',   'TNK', 'TANK'),
        ('230', 'lit',   'TNK', 'TANK'),
        ('247', 'lit',   'TNK', 'TANK'),
        ('250', 'lit',   'TNK', 'TANK'),
    ],
    'LIQUID_N2': [
        ('200', 'litre', 'TNK', 'TANK'),
        ('250', 'litre', 'TNK', 'TANK'),
    ],
    'PALLET': [
        ('105', 'cum',   'PLT', 'PALLET'),
    ],
    'CYLINDER': [
        ('7',   'cum',   'CYL', 'CYLINDER'),
        ('10',  'cum',   'CYL', 'CYLINDER'),
    ],
}

# Patterns to strip junk from raw Tally stock item names:
#   - "@ 18%", "18%", stray "%", "=" characters
_NAME_JUNK = re.compile(r'@\s*\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*%|=|%')

# For cylinders: strip embedded "7" or "10" (+ optional unit) from the name
# so we get a clean base — we will then add ALL cylinder variants ourselves.
_CYL_VARIANT_WITH_UNIT = re.compile(r'\b(10|7)\s*(?:cum|cub|cm|cubic)\b', re.IGNORECASE)
_CYL_VARIANT_BARE      = re.compile(r'\b(10|7)\b')


def _detect_product_type(base_name: str) -> str:
    """Classify a cleaned stock item name into a product type key."""
    nl = base_name.lower()
    if 'co2' in nl or 'carbon dioxide' in nl:
        return 'CO2'
    if 'liquid' in nl and ('oxygen' in nl or ' o2' in nl):
        return 'LIQUID_O2'
    if 'liquid' in nl and ('nitrogen' in nl or ' n2' in nl):
        return 'LIQUID_N2'
    if 'pallet' in nl or ' plt' in nl:
        return 'PALLET'
    return 'CYLINDER'


def parse_stock_item_name(name):
    """
    Clean a raw Tally stock item name and return its base name + product type.
    The caller iterates _PRODUCT_VARIANTS[product_type] to create all variant rows.

    Cleaning steps:
      1. Remove junk: =, @XX%, XX%, stray %
      2. For CYLINDER type: strip embedded "7"/"10" (+ optional unit suffix)
         so the base name doesn't already contain the variant size

    Returns dict with 'product_master_name' and 'product_type', or None for empty names.
    """
    if not name:
        return None

    # Strip junk and normalise whitespace
    clean = _NAME_JUNK.sub(' ', name)
    clean = ' '.join(clean.strip().split())
    if not clean:
        return None

    product_type      = _detect_product_type(clean)
    extracted_variant = None   # variant size found inside the name (cylinders only)

    # For cylinders strip any embedded size so base name is just the gas name
    if product_type == 'CYLINDER':
        m = _CYL_VARIANT_WITH_UNIT.search(clean) or _CYL_VARIANT_BARE.search(clean)
        if m:
            extracted_variant = m.group(1)          # "7" or "10"
            clean = (clean[:m.start()] + clean[m.end():]).strip()
            clean = ' '.join(clean.split())

    return {
        'product_master_name': clean,
        'product_type':        product_type,
        'extracted_variant':   extracted_variant,  # "7"/"10" if in name, else None
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
                base_name    = parsed['product_master_name']
                product_type = parsed['product_type']
                tally_guid   = product.get('guid', '')
                variants     = _PRODUCT_VARIANTS[product_type]

                logger.info(
                    f"[PARSED] '{product_name}' -> base='{base_name}', "
                    f"type={product_type}, variants={len(variants)}, "
                    f"HSN={product.get('hsn_code', '')}, GST={product.get('gst_rate', 0)}%"
                )

                # Save one row per variant
                for size, unit, type_code, type_name in variants:
                    variant_label  = f"{size}{unit}"
                    display_name   = f"{base_name} {variant_label} ({type_code})"
                    # Synthetic GUID: stock-item GUID + variant suffix → unique per row
                    variant_guid   = f"{tally_guid}|{type_code}_{size}{unit}" if tally_guid else ''

                    product_data = {
                        'tally_guid':          variant_guid,
                        'name':                display_name,
                        'name_canonical':      display_name,
                        'tally_company':       company_name,
                        'hsn_code':            product.get('hsn_code', ''),
                        'unit':                unit,
                        'rate':                product.get('rate', 0.0),
                        'description':         product.get('description', ''),
                        'data_json':           json.dumps(product),
                        'product_master_name': base_name,
                        'variant_name':        variant_label,
                        'unit_name':           unit,
                        'product_type_code':   type_code,
                        'product_type_name':   type_name,
                        'gst_applicable':      product.get('gst_applicable', ''),
                        'gst_rate':            product.get('gst_rate', 0.0),
                        'igst_rate':           product.get('igst_rate', 0.0),
                        'cgst_rate':           product.get('cgst_rate', 0.0),
                        'sgst_rate':           product.get('sgst_rate', 0.0),
                    }

                    try:
                        # Primary dedup: variant GUID
                        existing = db.product_exists_by_guid(variant_guid) if variant_guid else None
                        # Secondary dedup: same display name already saved
                        if not existing:
                            existing = db.product_exists_by_canonical(display_name)

                        if existing:
                            db.update_product(existing['id'], product_data)
                            logger.info(
                                f"  [UPDATED] '{display_name}' (SQLite ID: {existing['id']})"
                            )
                            overall_stats['duplicates_skipped'] += 1
                        else:
                            db.insert_product(product_data)
                            logger.info(f"  [NEW] '{display_name}'")
                            overall_stats['new_saved'] += 1

                    except Exception as e:
                        logger.error(
                            f"  [ERROR] Failed to save '{display_name}': {e}",
                            exc_info=True,
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
    logger.info(f"Stock items fetched from Tally: {overall_stats['total_fetched']}")
    logger.info(f"Stock items parsed:             {overall_stats['matched']}")
    logger.info(f"Skipped (empty name):           {overall_stats['skipped_no_type']}")
    logger.info(f"Variant rows — New:             {overall_stats['new_saved']}")
    logger.info(f"Variant rows — Updated/Dedup:   {overall_stats['duplicates_skipped']}")
    logger.info(f"Errors:                         {overall_stats['errors']}")
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
        logger.info("BHOX - PRODUCT FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        stats = fetch_products_from_all_companies()

        logger.info("\n✓ Product fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Product fetch failed: {e}", exc_info=True)
        exit(1)
