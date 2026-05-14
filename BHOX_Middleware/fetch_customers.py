"""
Fetch customers from multiple Tally companies (all ledger groups).
GUID-based create/update logic — existing customers are updated, new ones created.
Product/sales/expense ledgers are detected and skipped automatically.
"""
import json
import logging
import os
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
# Non-customer ledger detection + ledger-product variant saving
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

# Keywords found in custom sub-group names that still indicate non-customer ledgers
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
# e.g. "ARGON BHOX @ 18%", "NITROGEN CC @ 18 %"
_GST_RATE_PATTERN = re.compile(r'@\s*\d+(\.\d+)?\s*%\s*$')


def _is_non_customer_ledger(name: str, parent_group: str) -> bool:
    """Return True if this ledger is clearly NOT a customer (sales/expense/tax ledger)."""
    pg = (parent_group or '').strip().lower()

    # Exact match against known Tally non-customer top-level groups
    if pg in _NON_CUSTOMER_GROUPS:
        return True

    # Sub-group keyword match (catches custom sub-groups like "BHOX Gas Sales Accounts")
    for kw in _NON_CUSTOMER_GROUP_KEYWORDS:
        if kw in pg:
            return True

    # Name pattern: "PRODUCT NAME @ XX%" — GST-rate suffix means it's a sales/purchase ledger
    if _GST_RATE_PATTERN.search(name or ''):
        return True

    return False


# ---------------------------------------------------------------------------
# Ledger-product: variant definitions
# ---------------------------------------------------------------------------

# Each entry: (size_str, unit_abbr, type_code, type_name)
# Name format: "{base_name} {size}{unit} ({type_code})"
# Examples: "ARGON BHOX 7cum (CYL)", "LIQUID OXYGEN 200lit (TNK)", "PALLET 105cum (PLT)"
_LEDGER_PRODUCT_VARIANTS = {
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

# Extract GST rate from name suffix, e.g. "ARGON BHOX @ 18%" → 18.0
_GST_RATE_EXTRACT = re.compile(r'@\s*(\d+(?:\.\d+)?)\s*%\s*$')


def _extract_ledger_base_name(ledger_name: str) -> str:
    """Strip '@ XX%' GST-rate suffix to get the base product name."""
    return re.sub(r'\s*@\s*\d+(?:\.\d+)?\s*%\s*$', '', ledger_name or '').strip()


def _extract_gst_rate(ledger_name: str) -> float:
    m = _GST_RATE_EXTRACT.search(ledger_name or '')
    return float(m.group(1)) if m else 0.0


def _detect_ledger_product_type(base_name: str) -> str:
    """Classify base name into CO2 / LIQUID_O2 / LIQUID_N2 / PALLET / CYLINDER."""
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


def _save_ledger_product_variants(db, company_name: str, ledger: dict) -> int:
    """
    Parse a non-customer ledger as a product and save one row per variant.
    Returns the number of new variants inserted.
    """
    ledger_name = ledger.get('name', '')
    ledger_guid = ledger.get('guid', '')
    base_name   = _extract_ledger_base_name(ledger_name)
    gst_rate    = _extract_gst_rate(ledger_name)
    product_type = _detect_ledger_product_type(base_name)
    variants     = _LEDGER_PRODUCT_VARIANTS[product_type]
    new_count    = 0

    for size, unit, type_code, type_name in variants:
        variant_label = f"{size}{unit}"                            # e.g. "7cum", "200lit"
        full_name     = f"{base_name} {variant_label} ({type_code})"  # e.g. "ARGON BHOX 7cum (CYL)"
        # Synthetic GUID: ledger GUID + variant suffix so each row is unique
        variant_guid  = f"{ledger_guid}|{type_code}_{size}{unit}" if ledger_guid else ''
        canonical     = full_name

        product_data = {
            'tally_guid':           variant_guid,
            'name':                 full_name,
            'name_canonical':       canonical,
            'tally_company':        company_name,
            'hsn_code':             '',
            'unit':                 unit,
            'rate':                 0.0,
            'description':          f"Ledger: {ledger_name}",
            'data_json':            json.dumps(ledger),
            'product_master_name':  base_name,
            'variant_name':         variant_label,
            'unit_name':            unit,
            'product_type_code':    type_code,
            'product_type_name':    type_name,
            'gst_applicable':       'Applicable',
            'gst_rate':             gst_rate,
            'igst_rate':            gst_rate,
            'cgst_rate':            round(gst_rate / 2, 4),
            'sgst_rate':            round(gst_rate / 2, 4),
        }

        try:
            existing = db.product_exists_by_guid(variant_guid) if variant_guid else None
            if existing:
                db.update_product(existing['id'], product_data)
                logger.info(
                    f"[LEDGER PRODUCT UPDATED] '{full_name}' "
                    f"(type={type_name}, variant={variant_label})"
                )
            else:
                db.insert_product(product_data)
                logger.info(
                    f"[LEDGER PRODUCT NEW] '{full_name}' "
                    f"(type={type_name}, variant={variant_label}, GST={gst_rate}%)"
                )
                new_count += 1
        except Exception as e:
            logger.error(
                f"[ERROR] Failed to save ledger product '{full_name}': {e}",
                exc_info=True,
            )

    return new_count


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
            'errors': 0,
        }

    logger.info(f"Starting customer fetch from {len(active_companies)} companies")
    logger.info(f"Active companies: {', '.join(active_companies)}")

    overall_stats = {
        'total_fetched': 0,
        'new_saved': 0,
        'updated': 0,
        'errors': 0,
        'skipped_non_customer': 0,
        'ledger_products_saved': 0,
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
                parent_group = customer.get('parent_group', '')

                if not customer_name:
                    logger.warning("Skipping customer with empty name")
                    continue

                if _is_non_customer_ledger(customer_name, parent_group):
                    logger.info(
                        f"[NO-SEPARATION] '{customer_name}' (group: '{parent_group}') "
                        f"— saving in customers table (no product split)"
                    )

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
                                f"[UPDATED] '{customer_name}' (GUID: {tally_guid}, "
                                f"SQLite ID: {existing['id']})"
                            )
                            overall_stats['updated'] += 1
                        else:
                            logger.info(
                                f"[UNCHANGED] '{customer_name}' (GUID: {tally_guid}, "
                                f"SQLite ID: {existing['id']})"
                            )
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

    logger.info(f"\n{'='*60}")
    logger.info("CUSTOMER FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Fetched from Tally:   {overall_stats['total_fetched']}")
    logger.info(f"Ledger-products detected:   {overall_stats['skipped_non_customer']}")
    logger.info(f"Ledger-product rows saved:  {overall_stats['ledger_products_saved']}")
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
        logger.info("BHOX - CUSTOMER FETCH")
        logger.info(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60)

        stats = fetch_customers_from_all_companies()

        logger.info("\n✓ Customer fetch completed successfully")

    except Exception as e:
        logger.error(f"\n✗ Customer fetch failed: {e}", exc_info=True)
        exit(1)
