"""
Fetch invoices from multiple Tally companies with enhanced change detection.
- Stores complete Tally data (voucher, ledger, stock items) as JSON
- Computes payload hash for robust change detection (like CO_middleware)
- Supports soft delete tracking for deleted invoices
- Filters by config start date (from-date onward) and delivery information
"""
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta

from config import config, BASE_DIR
from db import Database, json_dumps, json_loads, sha256_text
from tally_client import TallyClient
from fetch_products import parse_stock_item_name, _PRODUCT_VARIANTS

# Try to import dashboard logger (optional - may not be available)
try:
    from log_capture import dashboard_logger
    HAS_DASHBOARD_LOGGER = True
except ImportError:
    HAS_DASHBOARD_LOGGER = False
    dashboard_logger = None

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def _attach_invoice_fetch_file_handler():
    """Log invoice fetch operations to a dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'invoice_fetch.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'invoice_fetch_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'invoice_fetch_file'
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_invoice_fetch_file_handler()

# Default date range constants (same as CO_middleware)
DEFAULT_DC_PAST_DAYS = 3
DEFAULT_DC_FUTURE_DAYS = 1


def _get_env_int(name, default):
    """Read an environment variable as int, falling back to *default*."""
    import os
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _default_date_range(days_back=None):
    """Return (from_date, to_date) as YYYYMMDD strings.

    Uses DC_PAST_DAYS / DC_FUTURE_DAYS env vars (same logic as CO_middleware).
    ``days_back`` parameter overrides DC_PAST_DAYS when provided.
    """
    now = datetime.now()
    if days_back is not None and days_back > 0:
        past = days_back
    else:
        past = _get_env_int("DC_PAST_DAYS", DEFAULT_DC_PAST_DAYS)
    future = _get_env_int("DC_FUTURE_DAYS", DEFAULT_DC_FUTURE_DAYS)
    from_dt = now - timedelta(days=past)
    to_dt = now + timedelta(days=future)
    return from_dt.strftime("%Y%m%d"), to_dt.strftime("%Y%m%d")


def _attach_invoice_fetch_error_handler():
    """Log invoice fetch errors to a dedicated file."""
    log_path = Path(BASE_DIR) / 'logs' / 'invoice_fetch_errors.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for handler in logger.handlers:
        if getattr(handler, 'name', '') == 'invoice_fetch_error_file':
            return
    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.name = 'invoice_fetch_error_file'
    fh.setLevel(logging.ERROR)
    fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S'))
    logger.addHandler(fh)


_attach_invoice_fetch_error_handler()

def log_message(message):
    """Log to both terminal and dashboard"""
    logger.info(message)
    if HAS_DASHBOARD_LOGGER and dashboard_logger:
        dashboard_logger.write_log(message)


def _compute_payload_hash(voucher, inventory_items, ledger_data, stock_items_map):
    """
    Compute SHA256 hash of complete invoice payload.
    Includes: voucher, inventory items, ledger data, stock item data.
    Hash changes trigger re-sync (like CO_middleware).
    """
    payload = {
        'voucher': voucher,
        'inventory': inventory_items or [],
        'ledger': ledger_data or {},
        'stock_items': stock_items_map or {},
    }
    payload_json = json_dumps(payload)
    return sha256_text(payload_json)


def _build_fallback_voucher(invoice):
    """Build a minimal voucher payload when full raw voucher is unavailable."""
    voucher = {
        'VOUCHERNUMBER': invoice.get('voucher_no', ''),
        'DATE': invoice.get('voucher_date', ''),
        'PARTYLEDGERNAME': invoice.get('customer_name', ''),
        'ADDRESSES': [invoice.get('billing_address', '')] if invoice.get('billing_address') else [],
        'INVENTORY': [],
        'LEDGERENTRIES': [],
    }

    delivery_address = invoice.get('delivery_address', '')
    if delivery_address:
        voucher['CONSIGNEE'] = {'ADDRESS': delivery_address}

    for item in invoice.get('items', []) or []:
        voucher['INVENTORY'].append({
            'STOCKITEMNAME': item.get('item_name', ''),
            'ACTUALQTY': str(item.get('quantity', 0)),
            'RATE': str(item.get('rate', 0)),
            'AMOUNT': str(item.get('amount', 0)),
        })

    total_amount = invoice.get('total_amount', 0.0)
    if total_amount:
        voucher['LEDGERENTRIES'].append({
            'LEDGERNAME': invoice.get('customer_name', ''),
            'AMOUNT': str(total_amount),
        })

    return voucher


def _build_full_voucher_payload(invoice):
    """Prefer full Tally voucher payload; fallback to normalized invoice reconstruction."""
    raw_voucher = invoice.get('raw_voucher')
    fallback_voucher = _build_fallback_voucher(invoice)

    if not isinstance(raw_voucher, dict) or not raw_voucher:
        return fallback_voucher

    voucher = dict(raw_voucher)

    voucher.setdefault('VOUCHERNUMBER', invoice.get('voucher_no', ''))
    voucher.setdefault('DATE', invoice.get('voucher_date', ''))
    voucher.setdefault('PARTYLEDGERNAME', invoice.get('customer_name', ''))

    if invoice.get('billing_address') and not voucher.get('ADDRESSES'):
        voucher['ADDRESSES'] = [invoice.get('billing_address')]

    if invoice.get('delivery_address') and not voucher.get('CONSIGNEE'):
        voucher['CONSIGNEE'] = {'ADDRESS': invoice.get('delivery_address')}

    if not voucher.get('INVENTORY'):
        voucher['INVENTORY'] = fallback_voucher.get('INVENTORY', [])

    if not voucher.get('LEDGERENTRIES') and fallback_voucher.get('LEDGERENTRIES'):
        voucher['LEDGERENTRIES'] = fallback_voucher['LEDGERENTRIES']

    return voucher


def _invoice_in_date_range(invoice, from_date, to_date):
    """Return True if voucher_date (YYYYMMDD) is on/after from_date."""
    vd = str(invoice.get('voucher_date', '') or '').replace('-', '').strip()
    if not vd or len(vd) != 8:
        return True
    return vd >= from_date


def _has_delivery_info(invoice):
    """
    Check Other Reference (BASICORDERREF / OTHERREFERENCE) for delivery/pickup keyword.

    If REQUIRE_DELIVERY_REF=false in .env this check is skipped and all invoices pass.

    Accepted values (when check is enabled):
      D / delivery / delivery challan / dispatch  → Delivery type
      C / customer pickup / pickup / self pickup  → Customer Pickup type
    """
    if not config.REQUIRE_DELIVERY_REF:
        return True

    raw = invoice.get('raw_voucher', {}) or {}
    other_ref = str(raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip()

    if not other_ref:
        return False

    other_ref_lower = other_ref.lower()
    other_ref_norm = ' '.join(other_ref_lower.split())
    other_ref_compact = other_ref_norm.replace(' ', '')

    if other_ref_norm == 'd' or other_ref_norm == 'c':
        return True

    if any(kw in other_ref_norm for kw in ('delivery', 'dispatch')):
        return True
    if 'deliverychallan' in other_ref_compact:
        return True

    if any(kw in other_ref_norm for kw in ('customer pickup', 'pickup', 'self pickup')):
        return True
    if 'customerpickup' in other_ref_compact:
        return True

    return False


def _is_instant_invoice(invoice):
    """Return True if the invoice appears to be an Instant DC invoice based on Tally fields."""
    raw = invoice.get('raw_voucher', {}) or {}
    vtype = str(raw.get('VOUCHERTYPENAME') or raw.get('VOUCHERTYPE') or '').strip().lower()
    if 'instant' in vtype:
        return True

    other_ref = str(raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip().lower()
    if 'instant' in other_ref:
        return True

    return False


def _normalize_name_key(value):
    return str(value or '').replace(' ', '').strip().lower()


def _parse_variant_token(token):
    """Parse '2X7', '2x7', '2X7.5' → (qty, size). Returns None if not a valid QxS token."""
    parts = token.upper().split('X')
    if len(parts) != 2:
        return None
    try:
        qty = int(parts[0])
        size = float(parts[1].strip())
        if size <= 0:
            return None
        return qty, size
    except ValueError:
        return None


# Business rule: 7.5 cum of gas is filled into a 10 cum cylinder — track as 10 cum.
_CYL_SIZE_REMAP = {7.5: 10.0}


def _normalize_cylinder_size(size):
    """Remap non-standard fill sizes to actual cylinder type.
    7.5 cum gas fill → 10 cum cylinder (per BHOX business rule)."""
    s = round(float(size), 2)
    return float(_CYL_SIZE_REMAP.get(s, s))


def _qty_from_description(item):
    """
    FUNCTION 1: Get cylinder quantity from BASICUSERDESCRIPTION.
    Called when the invoice item has a description field.

    Three formats:
      FORMAT 1  →  '3'          plain count; size inferred from ACTUALQTY÷count
      FORMAT 2  →  '5,3X7,2X10' total + QxS breakdown (total ignored, breakdown used)
      FORMAT 3  →  '3X7,2X10'   QxS breakdown only

    Returns expanded item list, or None if description is absent/unparseable.
    """
    user_desc = (item.get('user_description') or '').strip()
    if not user_desc:
        return None

    # Normalize newline separators Tally sometimes uses
    user_desc_normalized = re.sub(r'[\r\n]+', ',', user_desc)
    tokens = [t.strip() for t in user_desc_normalized.split(',') if t.strip()]
    if not tokens:
        return None

    orig_name = (item.get('item_name') or '').strip()
    base_name  = ' '.join(orig_name.replace('=', ' ').split())
    orig_qty   = item.get('quantity', 0.0) or 0.0
    orig_amt   = item.get('amount',   0.0) or 0.0

    base_name_l = base_name.lower()
    variant_unit = 'kg' if ('co2' in base_name_l or 'carbon dioxide' in base_name_l) else 'cum'

    def _make_item(variant_name, qty, rate, amt):
        return {
            'item_name':        variant_name,
            'quantity':         float(qty),
            'rate':             round(rate, 2),
            'amount':           round(amt,  2),
            'user_description': user_desc,
            '_orig_name':       orig_name,
        }

    def _build_multi(variant_tokens):
        """Build expanded list from QxS token strings (e.g. ['3X7', '2X10'])."""
        variants = []
        for tok in variant_tokens:
            parsed = _parse_variant_token(tok)
            if parsed is None:
                return None
            variants.append(parsed)
        total_cyls = sum(q for q, _ in variants)
        if total_cyls == 0:
            return None
        rate_per_cyl = (orig_amt / total_cyls) if total_cyls > 0 else (item.get('rate', 0.0) or 0.0)
        result = []
        for qty, size in variants:
            size = _normalize_cylinder_size(size)
            size_label = int(size) if size == int(size) else size
            result.append(_make_item(
                f"{base_name} {size_label}{variant_unit} (CYL)",
                qty,
                rate_per_cyl,
                qty * rate_per_cyl,
            ))
        return result

    first_as_variant = _parse_variant_token(tokens[0])

    if first_as_variant is None:
        # First token is a plain count (FORMAT 1 or FORMAT 2)
        try:
            total_count = int(tokens[0])
        except ValueError:
            return None
        if total_count <= 0:
            return None

        if len(tokens) > 1:
            # FORMAT 2: total + QxS breakdown — use breakdown, ignore total
            result = _build_multi(tokens[1:])
            return result  # None means unparseable → caller falls back to [item]

        # FORMAT 1: plain count only
        # Try to derive cylinder size from ACTUALQTY ÷ count
        rate_per_cyl = (orig_amt / total_count) if (orig_amt and total_count > 0) else (item.get('rate', 0.0) or 0.0)
        if orig_qty > 0:
            size_f = orig_qty / total_count
            size   = round(size_f, 2)
            if abs(size_f - size) <= 0.001:
                size = _normalize_cylinder_size(size)
                size_label = int(size) if size == int(size) else size
                return [_make_item(
                    f"{base_name} {size_label}{variant_unit} (CYL)",
                    total_count, rate_per_cyl, orig_amt,
                )]
        # Division not clean or qty=0 — trust count; DB lookup resolves variant name
        return [_make_item(base_name, total_count, rate_per_cyl, orig_amt)]
    else:
        # FORMAT 3: all tokens are QxS (no leading total)
        result = _build_multi(tokens)
        return result


def _qty_from_fields(item):
    """
    FUNCTION 2: Get cylinder quantity when NO description is present.

    Priority:
      1. nos_qty  — BILLEDQTY from Tally (explicit number-of-cylinders field)
      2. Infer from ACTUALQTY ÷ standard fill sizes (7, 7.5, 10 cum)

    Only applies to cylinder products (CO2/liquid/pallet are skipped).
    Returns expanded item list, or None if neither method works.
    """
    orig_name = (item.get('item_name') or '').strip()
    base_name = ' '.join(orig_name.replace('=', ' ').split())
    base_name_l = base_name.lower()

    # Skip non-cylinder product types
    if 'co2' in base_name_l or 'carbon dioxide' in base_name_l:
        return None
    if 'liquid' in base_name_l:
        return None
    if 'pallet' in base_name_l or ' plt' in base_name_l:
        return None

    amt  = float(item.get('amount')   or 0)
    rate = float(item.get('rate')     or 0)
    qty  = float(item.get('quantity') or 0)

    # --- Priority 1: BILLEDQTY / nos_qty (number of cylinders field) ---
    nos_qty = item.get('nos_qty')
    if nos_qty and float(nos_qty) > 0:
        count = int(round(float(nos_qty)))
        rate_per_cyl = (amt / count) if (amt and count > 0) else rate
        logger.debug(
            f"[NOS-QTY] '{orig_name}': nos_qty={count} cylinders"
        )
        return [{
            'item_name':        base_name,
            'quantity':         float(count),
            'rate':             round(rate_per_cyl, 2),
            'amount':           round(amt, 2),
            'user_description': '',
            '_orig_name':       orig_name,
        }]

    # --- Priority 2: Infer from ACTUALQTY ÷ standard fill sizes ---
    if qty <= 0:
        return None

    for raw_size in (7, 7.5, 10):
        count_f   = qty / raw_size
        count_int = round(count_f)
        if count_int > 0 and abs(count_f - count_int) < 0.001:
            size       = _normalize_cylinder_size(raw_size)
            size_label = int(size) if size == int(size) else size
            rate_per_cyl = (amt / count_int) if (amt and count_int > 0) else rate
            logger.debug(
                f"[INFER-VARIANT] '{orig_name}': qty={qty} / {raw_size} = {count_int} cyl "
                f"→ '{base_name} {size_label}cum (CYL)'"
            )
            return [{
                'item_name':        f"{base_name} {size_label}cum (CYL)",
                'quantity':         float(count_int),
                'rate':             round(rate_per_cyl, 2),
                'amount':           round(amt, 2),
                'user_description': '',
                '_orig_name':       orig_name,
            }]

    return None


def _expand_item_by_description(item):
    """
    Main entry point: expand a Tally inventory item into cylinder variant rows.

    Route:
      • Description present  → _qty_from_description()  (FUNCTION 1)
      • No description       → _qty_from_fields()        (FUNCTION 2)
      • Neither works        → return item unchanged
    """
    result = _qty_from_description(item)
    if result is not None:
        return result if result else [item]
    return _qty_from_fields(item) or [item]


def _is_customer_asset_product(name):
    """Return True if product name contains 'CC' as a standalone word token (customer asset).
    Handles variants: CC, cc, C.C, c.c, (CC), (C.C)
    """
    return 'cc' in [t.strip('()').replace('.', '') for t in (name or '').lower().split()]


def _find_product_in_db(db, item_name: str, normalized_item_name: str):
    """
    Two-step product lookup:
      1. Exact normalized name match  (legacy names / direct hit) — synced only
      2. Base-name match via product_master_name + optional variant filter
         (handles raw Tally names like 'HIGH PURE NITROGEN = BHOX' vs stored
          normalized names like 'HIGH PURE NITROGEN BHOX 7cum (CYL)')
    Returns data_json dict with '_db_name' set to the matched SQLite product name,
    or None if not found. The caller should use '_db_name' as the canonical item name.
    """
    def _attach_db_name(row, data):
        """Attach the SQLite product name to the returned dict so callers can normalize."""
        if data is not None:
            try:
                db_name = row['name']
                if db_name:
                    data['_db_name'] = db_name
            except (IndexError, KeyError):
                pass
        return data

    # Step 1: exact match on stored name — only accept if synced to Catalytics
    row = db.query(
        "SELECT name, data_json, is_synced FROM products WHERE lower(replace(name, ' ', '')) = ?",
        (normalized_item_name,)
    )
    if row and row['data_json'] and row['is_synced'] == 1:
        return _attach_db_name(row, json_loads(row['data_json']))

    # Step 2: parse to clean base name and match via product_master_name
    parsed = parse_stock_item_name(item_name)
    if not parsed:
        return None

    base_norm = _normalize_name_key(parsed['product_master_name'])
    extracted_v = parsed.get('extracted_variant')  # "7", "10", or None

    if extracted_v:
        # Try to match the specific variant (e.g. "7cum" starts with "7")
        row = db.query(
            "SELECT name, data_json FROM products "
            "WHERE lower(replace(product_master_name, ' ', '')) = ? "
            "AND variant_name LIKE ?",
            (base_norm, f"{extracted_v}%")
        )
    else:
        row = None

    if not (row and row['data_json']):
        # Fallback: any variant of this base product
        row = db.query(
            "SELECT name, data_json FROM products "
            "WHERE lower(replace(product_master_name, ' ', '')) = ?",
            (base_norm,)
        )

    if row and row['data_json']:
        return _attach_db_name(row, json_loads(row['data_json']))
    return None


def _auto_fetch_customer(db, tally, company_name, customer_name):
    """Auto-fetch a single customer from Tally and save to SQLite.
    Returns the customer data_json dict on success, None on failure."""
    import json as _json
    from tally_client import get_ledger_by_name

    try:
        ledger = get_ledger_by_name(company_name, customer_name, tally.url)
        if not ledger:
            logger.warning(f"[AUTO-FETCH] Customer '{customer_name}' not found in Tally")
            return None

        guid = ledger.get('GUID') or ledger.get('MASTERID') or ''
        name = (ledger.get('NAME') or customer_name).strip()
        normalized_name = ' '.join(name.split())

        customer_data = {
            'tally_guid': str(guid).strip(),
            'name': normalized_name,
            'tally_company': company_name,
            'gstin': (ledger.get('GSTIN') or ledger.get('PARTYGSTIN') or '').lstrip(':'),
            'pan': ledger.get('INCOMETAXNUMBER') or ledger.get('PANNUMBER') or '',
            'address': ', '.join(ledger.get('ADDRESSES', [])) if ledger.get('ADDRESSES') else '',
            'state': ledger.get('STATENAME') or '',
            'city': '',
            'pincode': ledger.get('PINCODE') or '',
            'phone': ledger.get('MOBILE') or ledger.get('LEDGERMOBILE') or '',
            'email': ledger.get('EMAIL') or ledger.get('LEDGEREMAIL') or '',
        }
        # Simple dict for data_json (same format as fetch_customers)
        simple_data = {
            'guid': customer_data['tally_guid'],
            'name': customer_data['name'],
            'parent_group': ledger.get('PARENT') or '',
            'gstin': customer_data['gstin'],
            'pan': customer_data['pan'],
            'address': customer_data['address'],
            'state': customer_data['state'],
            'city': '',
            'pincode': customer_data['pincode'],
            'phone': customer_data['phone'],
            'email': customer_data['email'],
        }
        customer_data['data_json'] = _json.dumps(simple_data)

        # GUID-based: update if exists, insert if new
        existing = db.customer_exists_by_guid(customer_data['tally_guid']) if customer_data['tally_guid'] else None
        if existing:
            db.update_customer(existing['id'], customer_data)
            logger.info(f"[AUTO-FETCH] Customer '{normalized_name}' updated in SQLite (GUID: {customer_data['tally_guid']})")
        else:
            db.insert_customer(customer_data)
            logger.info(f"[AUTO-FETCH] Customer '{normalized_name}' saved to SQLite (GUID: {customer_data['tally_guid']})")

        return simple_data

    except Exception as e:
        logger.error(f"[AUTO-FETCH] Failed to fetch customer '{customer_name}': {e}")
        return None


def _auto_fetch_product(db, tally, company_name, product_name):
    """Auto-fetch a single product from Tally and save to SQLite.
    Returns the product data_json dict on success, None on failure."""
    import json as _json
    from tally_client import get_stock_item_by_name

    try:
        # Tally usually stores base stock-item names, while invoice lines may contain
        # expanded middleware variant labels like "XYZ 7cum (CYL)".
        # Try exact first, then cleaned/base candidates.
        stock = None
        candidate_names = []
        seen_candidates = set()

        def _add_candidate(name):
            c = ' '.join((name or '').split()).strip()
            if not c:
                return
            key = c.lower()
            if key in seen_candidates:
                return
            seen_candidates.add(key)
            candidate_names.append(c)

        _add_candidate(product_name)
        _add_candidate(re.sub(r'\s+\d+\s*cum\s*\(CYL\)\s*$', '', product_name, flags=re.IGNORECASE))
        _add_candidate(re.sub(r'\s+\d+\s*(?:kg|lit|litre)\s*\((?:CYL|TNK|PLT)\)\s*$', '', product_name, flags=re.IGNORECASE))

        parsed_from_line = parse_stock_item_name(product_name)
        if parsed_from_line:
            _add_candidate(parsed_from_line.get('product_master_name'))

        for cand in candidate_names:
            stock = get_stock_item_by_name(company_name, cand, tally.url)
            if stock:
                if cand.lower() != ' '.join((product_name or '').split()).strip().lower():
                    logger.info(
                        f"[AUTO-FETCH] Product lookup fallback matched '{cand}' "
                        f"for invoice item '{product_name}'"
                    )
                break

        if not stock:
            logger.warning(f"[AUTO-FETCH] Product '{product_name}' not found in Tally")
            return None

        guid = stock.get('GUID') or stock.get('MASTERID') or ''
        name = (stock.get('NAME') or product_name).strip()

        # Parse product type + clean base name
        parsed = parse_stock_item_name(name) or {
            'product_master_name': name,
            'product_type': 'CYLINDER',
            'extracted_variant': None,
        }

        base_name    = parsed['product_master_name']
        product_type = parsed['product_type']
        tally_guid   = str(guid).strip()
        variants     = _PRODUCT_VARIANTS.get(product_type, _PRODUCT_VARIANTS['CYLINDER'])

        common = {
            'hsn_code':       stock.get('HSNCODE') or '',
            'unit':           stock.get('BASEUNITS') or '',
            'rate':           0.0,
            'gst_applicable': '',
            'gst_rate':       stock.get('GST_RATE') or 0.0,
            'igst_rate':      stock.get('IGST_RATE') or 0.0,
            'cgst_rate':      stock.get('CGST_RATE') or 0.0,
            'sgst_rate':      stock.get('SGST_RATE') or 0.0,
            'description':    stock.get('PARENT') or '',
        }

        # Create one row per variant (mirrors fetch_products behaviour)
        first_data_json = None
        created_product_ids = []
        for size, unit, type_code, type_name in variants:
            variant_label = f"{size}{unit}"
            display_name  = f"{base_name} {variant_label} ({type_code})"
            variant_guid  = f"{tally_guid}|{type_code}_{size}{unit}" if tally_guid else ''

            simple_data = dict(common, guid=variant_guid, name=display_name)
            if first_data_json is None:
                first_data_json = simple_data
                first_data_json['_db_name'] = display_name

            product_data = {
                'tally_guid':          variant_guid,
                'name':                display_name,
                'name_canonical':      display_name,
                'tally_company':       company_name,
                'hsn_code':            common['hsn_code'],
                'unit':                unit,
                'rate':                common['rate'],
                'description':         common['description'],
                'data_json':           _json.dumps(simple_data),
                'product_master_name': base_name,
                'variant_name':        variant_label,
                'unit_name':           unit,
                'product_type_code':   type_code,
                'product_type_name':   type_name,
                'gst_applicable':      common['gst_applicable'],
                'gst_rate':            common['gst_rate'],
                'igst_rate':           common['igst_rate'],
                'cgst_rate':           common['cgst_rate'],
                'sgst_rate':           common['sgst_rate'],
            }

            existing = db.product_exists_by_guid(variant_guid) if variant_guid else None
            if not existing:
                existing = db.product_exists_by_canonical(display_name)
            if existing:
                db.update_product(existing['id'], product_data)
                created_product_ids.append(existing['id'])
                logger.info(f"[AUTO-FETCH] Product '{display_name}' updated (ID: {existing['id']})")
            else:
                new_id = db.insert_product(product_data)
                if new_id:
                    created_product_ids.append(new_id)
                logger.info(f"[AUTO-FETCH] Product '{display_name}' saved to SQLite")

        # Return data for the variant that matches the requested product_name.
        # e.g. "ACM GAS 10 CUM BHOX 10cum (CYL)" should return the 10cum variant,
        # not always the first (7cum) variant.
        target_parsed = parse_stock_item_name(product_name)
        target_variant = target_parsed.get('extracted_variant') if target_parsed else None

        if target_variant:
            for size, unit, type_code, _ in variants:
                if str(size) == str(target_variant):
                    variant_label = f"{size}{unit}"
                    target_name = f"{base_name} {variant_label} ({type_code})"
                    variant_guid = f"{tally_guid}|{type_code}_{size}{unit}" if tally_guid else ''
                    matched_data = dict(common, guid=variant_guid, name=target_name)
                    matched_data['_db_name'] = target_name
                    matched_data['_created_product_ids'] = created_product_ids
                    return matched_data

            # target_variant not in predefined variants (e.g. "7.5" not in [7, 10]).
            # Create a custom variant row for this specific non-standard size.
            if product_type == 'CYLINDER':
                try:
                    size_val = float(target_variant)
                    size_label = int(size_val) if size_val == int(size_val) else size_val
                    variant_label = f"{size_label}cum"
                    target_name = f"{base_name} {variant_label} (CYL)"
                    variant_guid = f"{tally_guid}|CYL_{size_label}cum" if tally_guid else ''
                    custom_product_data = {
                        'tally_guid':          variant_guid,
                        'name':                target_name,
                        'name_canonical':      target_name,
                        'tally_company':       company_name,
                        'hsn_code':            common['hsn_code'],
                        'unit':                'cum',
                        'rate':                common['rate'],
                        'description':         common['description'],
                        'data_json':           _json.dumps(dict(common, guid=variant_guid, name=target_name)),
                        'product_master_name': base_name,
                        'variant_name':        variant_label,
                        'unit_name':           'cum',
                        'product_type_code':   'CYL',
                        'product_type_name':   'CYLINDER',
                        'gst_applicable':      common['gst_applicable'],
                        'gst_rate':            common['gst_rate'],
                        'igst_rate':           common['igst_rate'],
                        'cgst_rate':           common['cgst_rate'],
                        'sgst_rate':           common['sgst_rate'],
                    }
                    existing = db.product_exists_by_guid(variant_guid) if variant_guid else None
                    if not existing:
                        existing = db.product_exists_by_canonical(target_name)
                    if existing:
                        db.update_product(existing['id'], custom_product_data)
                        created_product_ids.append(existing['id'])
                        logger.info(f"[AUTO-FETCH] Custom variant '{target_name}' updated (ID: {existing['id']})")
                    else:
                        new_id = db.insert_product(custom_product_data)
                        if new_id:
                            created_product_ids.append(new_id)
                        logger.info(f"[AUTO-FETCH] Custom variant '{target_name}' saved to SQLite")
                    matched_data = dict(common, guid=variant_guid, name=target_name)
                    matched_data['_db_name'] = target_name
                    matched_data['_created_product_ids'] = created_product_ids
                    return matched_data
                except Exception as e:
                    logger.warning(f"[AUTO-FETCH] Could not create custom variant for size '{target_variant}': {e}")

        if first_data_json is not None:
            first_data_json['_created_product_ids'] = created_product_ids
        return first_data_json

    except Exception as e:
        logger.error(f"[AUTO-FETCH] Failed to fetch product '{product_name}': {e}")
        return None


def _process_invoice_batch(db, tally, company_name, invoices, from_date, to_date, ledger_cache, stock_cache, overall_stats, syncer=None):
    """Process a batch of invoices and save to database."""
    overall_stats['total_fetched'] += len(invoices)

    for invoice in invoices:
        voucher_no = invoice.get('voucher_no', '')
        customer_name = invoice.get('customer_name', '')
        voucher_date = invoice.get('voucher_date', '')
        auto_created_customer_id = None
        auto_created_product_ids = []

        if not voucher_no or not customer_name:
            logger.warning(
                f"[SKIP:EMPTY] Invoice skipped — "
                f"voucher_no={'(empty)' if not voucher_no else voucher_no}, "
                f"customer={'(empty)' if not customer_name else customer_name}"
            )
            continue

        if not _invoice_in_date_range(invoice, from_date, to_date):
            logger.info(
                f"[SKIP:DATE] #{voucher_no} | date={voucher_date} | "
                f"customer='{customer_name}' | reason: before from_date {from_date}"
            )
            overall_stats['skipped_date'] += 1
            continue

        if not _has_delivery_info(invoice):
            raw = invoice.get('raw_voucher', {}) or {}
            vtype = str(raw.get('VOUCHERTYPENAME') or raw.get('VOUCHERTYPE') or '').strip()
            other_ref = str(raw.get('BASICORDERREF') or raw.get('OTHERREFERENCE') or '').strip()
            logger.info(
                f"[SKIP:NO_DELIVERY] #{voucher_no} | date={voucher_date} | "
                f"customer='{customer_name}' | voucher_type='{vtype}' | "
                f"other_ref='{other_ref}'"
            )
            overall_stats['skipped_no_delivery'] += 1
            continue

        normalized_customer = _normalize_name_key(customer_name)
        ledger_cache_key = f"{company_name}::{normalized_customer}"
        if ledger_cache_key not in ledger_cache:
            try:
                row = db.query("SELECT name, data_json FROM customers WHERE lower(replace(name, ' ', '')) = ?", (normalized_customer,))
                ledger_cache[ledger_cache_key] = json_loads(row['data_json']) if (row and row['data_json']) else None
            except Exception:
                ledger_cache[ledger_cache_key] = None

        ledger_data = ledger_cache[ledger_cache_key]
        if not ledger_data:
            # Auto-fetch customer from Tally and save to SQLite
            logger.info(
                f"[AUTO-FETCH:CUSTOMER] #{voucher_no} | customer='{customer_name}' "
                f"not in SQLite — fetching from Tally..."
            )
            fetched = _auto_fetch_customer(db, tally, company_name, customer_name)
            if fetched:
                ledger_cache[ledger_cache_key] = fetched
                ledger_data = fetched
                overall_stats.setdefault('auto_fetched_customers', 0)
                overall_stats['auto_fetched_customers'] += 1
                try:
                    cust_row = db.query(
                        "SELECT id FROM customers WHERE tally_company = ? AND lower(replace(name, ' ', '')) = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (company_name, normalized_customer),
                    )
                    if cust_row and cust_row['id']:
                        auto_created_customer_id = cust_row['id']
                except Exception:
                    pass
            else:
                logger.warning(
                    f"[SKIP:MISSING_CUSTOMER] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | reason: not found in SQLite or Tally"
                )
                overall_stats['skipped_missing_customer'] += 1
                continue

        inventory_items = invoice.get('items', [])

        # Skip DCs that contain customer-asset products (items with 'CC' as a word in name)
        cc_items = [
            (item.get('item_name') or '').strip()
            for item in inventory_items
            if _is_customer_asset_product(item.get('item_name') or '')
        ]
        if cc_items:
            logger.info(
                f"[SKIP:CUSTOMER_ASSET] #{voucher_no} | date={voucher_date} | "
                f"customer='{customer_name}' | reason: contains customer-asset CC product(s): {cc_items}"
            )
            overall_stats.setdefault('skipped_customer_asset', 0)
            overall_stats['skipped_customer_asset'] += 1
            continue

        # Expand items that have cylinder variant description (e.g. '10,2X7,8X10')
        expanded_items = []
        has_expansion = False
        for item in inventory_items:
            expanded = _expand_item_by_description(item)
            if len(expanded) > 1 or (len(expanded) == 1 and expanded[0].get('_orig_name')):
                has_expansion = True
            expanded_items.extend(expanded)

        if has_expansion:
            inventory_items = expanded_items
            # Rebuild raw_voucher INVENTORY to match expanded items.
            # Use a deque per orig_name so each raw line consumes only its OWN
            # expanded items — prevents duplication when the same product appears
            # on multiple Tally lines (e.g. same gas at two different rates).
            from collections import deque
            orig_to_expanded = {}
            for exp in expanded_items:
                key = exp.get('_orig_name', exp['item_name'])
                if key not in orig_to_expanded:
                    orig_to_expanded[key] = deque()
                orig_to_expanded[key].append(exp)
            raw_inv = invoice.get('raw_voucher', {}).get('INVENTORY') or []
            new_raw_inv = []
            for raw_item in raw_inv:
                orig_name = (raw_item.get('STOCKITEMNAME') or '').strip()
                queue = orig_to_expanded.get(orig_name)
                if queue:
                    # Pop only the next expanded item(s) that belong to this raw line.
                    # FORMAT 1 (plain number like "10", "5") → always 1 item per raw line.
                    # FORMAT 2/3 (comma description like "3X30,2X30") → N items per raw line,
                    # all sharing the same user_description with commas.
                    exp0 = queue.popleft()
                    batch = [exp0]
                    desc0 = exp0.get('user_description', '')
                    # Only keep collecting if this is a multi-split (description has commas).
                    # Avoids mis-grouping two separate raw lines that both have the same plain description.
                    if ',' in desc0:
                        while queue and queue[0].get('user_description', '') == desc0:
                            batch.append(queue.popleft())
                    for exp in batch:
                        new_item = dict(raw_item)
                        new_item['STOCKITEMNAME'] = exp['item_name']
                        new_item['ACTUALQTY'] = str(exp['quantity'])
                        new_item['BILLEDQTY'] = str(exp['quantity'])
                        new_item['RATE'] = str(exp['rate'])
                        new_item['AMOUNT'] = str(exp['amount'])
                        new_raw_inv.append(new_item)
                else:
                    new_raw_inv.append(raw_item)
            invoice['raw_voucher']['INVENTORY'] = new_raw_inv
            logger.info(
                f"[EXPAND:DESCRIPTION] #{voucher_no} | expanded {len(raw_inv)} item(s) → "
                f"{len(new_raw_inv)} variant item(s) using description '{expanded_items[0].get('user_description', '')}'"
            )

        stock_items_map = {}
        missing_products = []
        for item in inventory_items:
            item_name = (item.get('item_name') or '').strip()
            normalized_item_name = _normalize_name_key(item_name)
            stock_cache_key = f"{company_name}::{normalized_item_name}"
            if stock_cache_key not in stock_cache:
                try:
                    stock_cache[stock_cache_key] = _find_product_in_db(db, item_name, normalized_item_name)
                except Exception:
                    stock_cache[stock_cache_key] = None

            product_data = stock_cache.get(stock_cache_key)
            if product_data:
                # Use the matched DB name (normalized, without = and extra spaces)
                # so that STOCKITEMNAME and stock_items_map key match Catalytics product names
                db_name = product_data.get('_db_name') or item_name
                if db_name != item_name:
                    logger.info(
                        f"[NAME-NORMALIZE] #{voucher_no} | '{item_name}' → '{db_name}'"
                    )
                    item['item_name'] = db_name
                    # Update raw_voucher INVENTORY STOCKITEMNAME to match
                    for raw_inv in (invoice.get('raw_voucher', {}).get('INVENTORY') or []):
                        if (raw_inv.get('STOCKITEMNAME') or '').strip() == item_name:
                            raw_inv['STOCKITEMNAME'] = db_name
                            break
                stock_items_map[db_name] = product_data
            else:
                missing_products.append(item_name)

        # Auto-fetch missing products from Tally
        if missing_products:
            missing_products = list(dict.fromkeys(missing_products))
            still_missing = []
            fetched_any_product = False
            fetched_product_ids = []
            for mp_name in missing_products:
                logger.info(
                    f"[AUTO-FETCH:PRODUCT] #{voucher_no} | product='{mp_name}' "
                    f"not in SQLite — fetching from Tally..."
                )
                fetched = _auto_fetch_product(db, tally, company_name, mp_name)
                if fetched:
                    db_name = fetched.get('_db_name') or mp_name
                    normalized_mp = _normalize_name_key(db_name)
                    cache_key = f"{company_name}::{normalized_mp}"
                    stock_cache[cache_key] = fetched
                    stock_items_map[db_name] = fetched
                    overall_stats.setdefault('auto_fetched_products', 0)
                    overall_stats['auto_fetched_products'] += 1
                    fetched_any_product = True
                    created_ids = fetched.get('_created_product_ids') or []
                    for pid in created_ids:
                        if pid:
                            fetched_product_ids.append(pid)
                            auto_created_product_ids.append(pid)
                else:
                    still_missing.append(mp_name)

            if syncer and (auto_created_customer_id or auto_created_product_ids):
                try:
                    if auto_created_customer_id:
                        cust_row = db.query_all(
                            "SELECT * FROM customers WHERE id = ? LIMIT 1",
                            (auto_created_customer_id,)
                        )
                        if cust_row:
                            sync_res = syncer.sync_single_customer(dict(cust_row[0]))
                            if sync_res.get('success'):
                                db.mark_customer_synced(
                                    auto_created_customer_id,
                                    sync_res.get('catalytics_id'),
                                    json_dumps(sync_res)
                                )
                                logger.info(
                                    f"[AUTO-SYNC:CUSTOMER] #{voucher_no} | customer='{customer_name}' synced to server "
                                    f"(id={sync_res.get('catalytics_id')})"
                                )
                            else:
                                # User-requested behavior: treat auto-created records as synced locally.
                                db.mark_customer_synced(auto_created_customer_id, None, json_dumps(sync_res))
                                logger.warning(
                                    f"[AUTO-SYNC:CUSTOMER] #{voucher_no} | customer='{customer_name}' sync failed: "
                                    f"{sync_res.get('error')}"
                                )
                                logger.warning(
                                    f"[AUTO-SYNC:CUSTOMER:DETAIL] #{voucher_no} | entity_id={syncer.entity_id} | "
                                    f"endpoint='/import/tally-customer-payload/' | response={json_dumps(sync_res)}"
                                )

                    auto_created_product_ids = list(dict.fromkeys(auto_created_product_ids))
                    prod_rows = []
                    if auto_created_product_ids:
                        placeholders = ",".join(["?"] * len(auto_created_product_ids))
                        prod_rows = db.query_all(
                            f"SELECT * FROM products WHERE id IN ({placeholders})",
                            tuple(auto_created_product_ids)
                        )

                    batch_items = []
                    batch_products = []
                    for prod in prod_rows:
                        stock_item, err = syncer._prepare_stock_item(prod)
                        if not stock_item:
                            logger.warning(
                                f"[AUTO-SYNC:PRODUCT] #{voucher_no} | skip '{prod.get('name', '')}': {err}"
                            )
                            continue
                        batch_items.append(stock_item)
                        batch_products.append(prod)

                    if batch_items:
                        payload = {
                            'entity_id': syncer.entity_id,
                            'stock_items': batch_items,
                            'created_by': config.DEFAULT_ADMIN_USER_ID,
                        }
                        resp = syncer._api_request(
                            'POST',
                            '/import/tally-product-payload/',
                            json=payload,
                            timeout=config.API_TIMEOUT_BATCH_SYNC,
                        )
                        if resp.status_code in [200, 201]:
                            result = resp.json()
                            data = result.get('data', result)
                            results_list = data.get('results', [])
                            synced_cnt = 0
                            failed_cnt = 0
                            for i, prod in enumerate(batch_products):
                                row_res = results_list[i] if i < len(results_list) else {}
                                status = row_res.get('status', 'error')
                                if status in ('created', 'updated', 'skipped'):
                                    catalytics_id = (
                                        row_res.get('product_id')
                                        or row_res.get('id')
                                        or row_res.get('stock_item_id')
                                    )
                                    db.mark_product_synced(prod['id'], catalytics_id, json_dumps(row_res))
                                    synced_cnt += 1
                                else:
                                    db.mark_product_synced(prod['id'], None, json_dumps(row_res))
                                    failed_cnt += 1
                            logger.info(
                                f"[AUTO-SYNC:PRODUCT] #{voucher_no} | immediate sync for fetched products "
                                f"(synced={synced_cnt}, failed={failed_cnt})"
                            )
                        else:
                            resp_text = (resp.text or '').strip()
                            for prod in batch_products:
                                db.mark_product_synced(
                                    prod['id'],
                                    None,
                                    json_dumps({'status': 'forced_local_sync', 'http_status': resp.status_code})
                                )
                            logger.warning(
                                f"[AUTO-SYNC:PRODUCT] #{voucher_no} | HTTP {resp.status_code} for immediate product sync"
                            )
                            logger.warning(
                                f"[AUTO-SYNC:PRODUCT:DETAIL] #{voucher_no} | entity_id={syncer.entity_id} | "
                                f"endpoint='/import/tally-product-payload/' | status={resp.status_code} | "
                                f"response={resp_text}"
                            )
                    else:
                        logger.info(
                            f"[AUTO-SYNC:PRODUCT] #{voucher_no} | no prepared fetched products for immediate sync"
                        )
                except Exception as sync_err:
                    if auto_created_customer_id:
                        db.mark_customer_synced(
                            auto_created_customer_id,
                            None,
                            json_dumps({'status': 'forced_local_sync', 'error': str(sync_err)})
                        )
                    for pid in list(dict.fromkeys(auto_created_product_ids)):
                        db.mark_product_synced(
                            pid,
                            None,
                            json_dumps({'status': 'forced_local_sync', 'error': str(sync_err)})
                        )
                    logger.error(
                        f"[AUTO-SYNC] #{voucher_no} | immediate customer/product sync error: {sync_err}"
                    )
                    logger.error(
                        f"[AUTO-SYNC:DETAIL] #{voucher_no} | entity_id={getattr(syncer, 'entity_id', None)} | "
                        f"customer_id={auto_created_customer_id} | product_ids={auto_created_product_ids}"
                    )

            if still_missing:
                logger.warning(
                    f"[SKIP:MISSING_PRODUCT] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | missing {len(still_missing)} product(s) "
                    f"(not found in SQLite or Tally): {still_missing}"
                )
                overall_stats['skipped_missing_product'] += 1
                continue

        try:
            full_voucher_payload = _build_full_voucher_payload(invoice)
            enriched_voucher = dict(full_voucher_payload)
            if ledger_data: enriched_voucher['LEDGERDATA'] = ledger_data
            if stock_items_map: enriched_voucher['STOCKITEMS'] = stock_items_map
            
            # Enrich items with GST metadata
            inv_lines = enriched_voucher.get('INVENTORY') or []
            if isinstance(inv_lines, list):
                for inv in inv_lines:
                    if not isinstance(inv, dict): continue
                    st_name = (inv.get('STOCKITEMNAME') or inv.get('ITEMNAME') or '').strip()
                    st_data = stock_items_map.get(st_name) or {}
                    if not st_data: continue
                    for k in ('GST_RATE', 'IGST_RATE', 'CGST_RATE', 'SGST_RATE', 'HSNCODE'):
                        if k in st_data: inv[k] = st_data[k]

            data_json = json_dumps(enriched_voucher)
            payload_hash = _compute_payload_hash(full_voucher_payload, inventory_items, ledger_data, stock_items_map)

            # Extract Godown/Filling Station info for DB columns
            g_name = enriched_voucher.get('GODOWNNAME') or ''
            l_name = enriched_voucher.get('LOCATIONNAME') or ''
            f_station = enriched_voucher.get('FILLINGSTATION') or ''
            
            # If all empty at voucher level, pick from first inventory row
            if not g_name and not l_name and not f_station:
                for inv in enriched_voucher.get('INVENTORY', []) or []:
                    if not isinstance(inv, dict): continue
                    g_name = inv.get('GODOWNNAME') or ''
                    l_name = inv.get('LOCATIONNAME') or ''
                    if g_name or l_name: break

            invoice_record = {
                'voucher_no': voucher_no,
                'tally_company': company_name,
                'tally_guid': invoice.get('guid', ''),
                'voucher_date': invoice.get('voucher_date', ''),
                'customer_name': customer_name,
                'customer_guid': invoice.get('customer_guid', ''),
                'billing_address': invoice.get('billing_address', ''),
                'delivery_address': invoice.get('delivery_address', ''),
                'total_amount': invoice.get('total_amount', 0.0),
                'tax_amount': invoice.get('tax_amount', 0.0),
                'items_json': json_dumps(inventory_items),
                'data_json': data_json,
                'ledger_data_json': json_dumps(ledger_data),
                'stock_items_json': json_dumps(stock_items_map),
                'payload_hash': payload_hash,
                'godown_name': g_name,
                'location_name': l_name,
                'filling_station': f_station,
                'is_instant': 1 if _is_instant_invoice(invoice) else 0
            }

            existing = db.invoice_exists(voucher_no, company_name)
            if not existing:
                db.insert_invoice(invoice_record)
                logger.info(
                    f"[NEW] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | items={len(inventory_items)} | "
                    f"amount={invoice.get('total_amount', 0.0)}"
                )
                overall_stats['new_saved'] += 1
            elif (existing['payload_hash'] != payload_hash) or (existing['data_json'] != data_json):
                db.update_invoice(existing['id'], invoice_record)
                logger.info(
                    f"[UPDATED] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | SQLite ID={existing['id']} | "
                    f"reason: payload changed"
                )
                overall_stats['updated_saved'] += 1
            else:
                logger.debug(
                    f"[UNCHANGED] #{voucher_no} | date={voucher_date} | "
                    f"customer='{customer_name}' | no changes"
                )
                overall_stats['already_exists'] += 1

        except Exception as e:
            logger.error(
                f"[ERROR] #{voucher_no} | date={voucher_date} | "
                f"customer='{customer_name}' | error: {e}",
                exc_info=True
            )
            overall_stats['errors'] += 1


def fetch_invoices_from_all_companies(from_date=None, to_date=None):
    """Fetch invoices from all active Tally companies in monthly batches."""
    try:
        config.reload_from_env()
    except AttributeError:
        pass

    # Date range logic (same as CO_middleware):
    #   - If both from_date & to_date supplied → use them (user-specified range)
    #   - Otherwise → use _default_date_range() with DC_PAST_DAYS / DC_FUTURE_DAYS
    user_specified_range = bool(from_date and to_date)
    if user_specified_range:
        logger.info(f"Using user-specified date range {from_date} to {to_date}")
    else:
        from_date, to_date = _default_date_range()
        logger.info(f"Using default date range {from_date} to {to_date} "
                     f"(DC_PAST_DAYS={_get_env_int('DC_PAST_DAYS', DEFAULT_DC_PAST_DAYS)}, "
                     f"DC_FUTURE_DAYS={_get_env_int('DC_FUTURE_DAYS', DEFAULT_DC_FUTURE_DAYS)})")

    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
    try:
        from sync_to_catalytics import CatalyticsSyncer
        syncer = CatalyticsSyncer()
    except Exception as e:
        logger.warning(f"Could not initialize SyncEngine for immediate customer/product sync: {e}")
        syncer = None
    active_companies = config.get_active_companies()

    if not active_companies:
        logger.error("No active Tally companies configured")
        return {'total_fetched': 0, 'new_saved': 0, 'updated_saved': 0, 'already_exists': 0, 'errors': 0}

    logger.info(f"Starting batched invoice fetch from {len(active_companies)} companies")
    overall_stats = {'total_fetched': 0, 'skipped_date': 0, 'skipped_no_delivery': 0, 'skipped_missing_customer': 0, 'skipped_missing_product': 0, 'new_saved': 0, 'updated_saved': 0, 'already_exists': 0, 'deleted': 0, 'errors': 0}
    
    ledger_cache = {}
    stock_cache = {}

    for company_name in active_companies:
        logger.info(f"\n{'='*60}\nProcessing: {company_name}\n{'='*60}")
        
        try:
            from_dt = datetime.strptime(from_date, '%Y%m%d')
            to_dt = datetime.strptime(to_date, '%Y%m%d') if to_date != '20991231' else datetime.now()
            
            curr_start = from_dt
            while curr_start <= to_dt:
                curr_end = curr_start + timedelta(days=29)
                if curr_end > to_dt: curr_end = to_dt
                
                s_date = curr_start.strftime('%Y%m%d')
                e_date = curr_end.strftime('%Y%m%d')
                
                logger.info(f" -> Batch: {s_date} to {e_date}")
                try:
                    invoices = tally.get_sales_invoices(company_name, from_date=s_date, to_date=e_date)
                    if invoices:
                        _process_invoice_batch(
                            db, tally, company_name, invoices, from_date, to_date,
                            ledger_cache, stock_cache, overall_stats, syncer=syncer
                        )
                except Exception as b_err:
                    logger.error(f"Batch failed: {b_err}")
                
                curr_start = curr_start + timedelta(days=30)
                import time
                time.sleep(config.INVOICE_BATCH_SLEEP_SECONDS)

        except Exception as e:
            logger.error(f"Company process failed: {e}")
            overall_stats['errors'] += 1

    logger.info(f"\n{'='*60}")
    logger.info("FINAL FETCH SUMMARY")
    logger.info(f"{'='*60}")
    logger.info(f"Total Fetched:       {overall_stats['total_fetched']}")
    logger.info(f"New Saved:           {overall_stats['new_saved']}")
    logger.info(f"Updated Saved:       {overall_stats['updated_saved']}")
    logger.info(f"Auto-Fetch Cust:     {overall_stats.get('auto_fetched_customers', 0)}")
    logger.info(f"Auto-Fetch Prod:     {overall_stats.get('auto_fetched_products', 0)}")
    logger.info(f"Skipped Date:        {overall_stats['skipped_date']}")
    logger.info(f"Skipped No Del:      {overall_stats['skipped_no_delivery']}")
    logger.info(f"Skipped Cust:        {overall_stats['skipped_missing_customer']}")
    logger.info(f"Skipped Prod:        {overall_stats['skipped_missing_product']}")
    logger.info(f"Already Exist:       {overall_stats['already_exists']}")
    logger.info(f"Errors:              {overall_stats['errors']}")
    logger.info(f"{'='*60}")
    
    db.close()
    return overall_stats


if __name__ == '__main__':
    import sys
    try:
        from_date = sys.argv[1] if len(sys.argv) >= 2 else None
        to_date = sys.argv[2] if len(sys.argv) >= 3 else None
        
        if not from_date and not to_date:
            logger.info("Initializing BHOX - DAY BOOK FETCH (Yesterday to Tomorrow)")
        elif from_date and not to_date:
            logger.info(f"BHOX - INVOICE FETCH: from {from_date} onwards")
        else:
            logger.info(f"BHOX - INVOICE FETCH: {from_date} to {to_date}")

        fetch_invoices_from_all_companies(from_date, to_date)
    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        exit(1)
