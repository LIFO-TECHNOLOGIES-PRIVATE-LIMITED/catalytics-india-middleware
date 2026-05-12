"""
Fetch invoices from multiple Tally companies with enhanced change detection.
- Stores complete Tally data (voucher, ledger, stock items) as JSON
- Computes payload hash for robust change detection (like CO_middleware)
- Supports soft delete tracking for deleted invoices
- Filters by config start date (from-date onward) and delivery information
"""
import logging
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
    """Parse '2X7' or '2x7' → (qty, size). Returns None if not a valid QxS token."""
    parts = token.upper().split('X')
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _expand_item_by_description(item):
    """
    Expand one Tally item into variant items using BASICUSERDESCRIPTION.

    Three formats are supported:

    1. Total count only  →  '10'
       size = Tally_qty / 10  →  single item with correct variant name + cylinder qty

    2. Total + breakdown  →  '10,3X10,7X7'
       Expand into 2 items: 3 cylinders of 10cum, 7 cylinders of 7cum

    3. Breakdown only (no total prefix)  →  '3X30,2X30'
       Same as format 2 but first token is already a QxS pair

    Returns expanded list, or [item] unchanged if format is not valid.
    """
    user_desc = (item.get('user_description') or '').strip()
    if not user_desc:
        return [item]

    tokens = [t.strip() for t in user_desc.split(',') if t.strip()]
    if not tokens:
        return [item]

    orig_name = (item.get('item_name') or '').strip()
    base_name  = ' '.join(orig_name.replace('=', ' ').split())
    orig_qty   = item.get('quantity', 0.0) or 0.0
    orig_amt   = item.get('amount',   0.0) or 0.0

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
        """Build expanded list from a list of QxS token strings."""
        variants = []
        for tok in variant_tokens:
            parsed = _parse_variant_token(tok)
            if parsed is None:
                return None          # invalid token → abort expansion
            variants.append(parsed)
        total_cyls = sum(q for q, _ in variants)
        if total_cyls == 0:
            return None
        rate_per_cyl = (orig_amt / total_cyls) if total_cyls > 0 else (item.get('rate', 0.0) or 0.0)
        result = []
        for qty, size in variants:
            result.append(_make_item(
                f"{base_name} {size}cum (CYL)",
                qty,
                rate_per_cyl,
                qty * rate_per_cyl,
            ))
        return result

    # --- Determine format ---
    first_as_variant = _parse_variant_token(tokens[0])

    if first_as_variant is None:
        # First token is a plain number (total count)
        try:
            total_count = int(tokens[0])
        except ValueError:
            return [item]

        if total_count <= 0:
            return [item]

        if len(tokens) == 1:
            # FORMAT 1: total count only → infer size from Tally qty
            if orig_qty <= 0:
                return [item]
            size_f = orig_qty / total_count
            size   = int(size_f)
            if abs(size_f - size) > 0.001:          # not a clean division
                return [item]
            rate_per_cyl = (orig_amt / total_count) if total_count > 0 else (item.get('rate', 0.0) or 0.0)
            return [_make_item(
                f"{base_name} {size}cum (CYL)",
                total_count,
                rate_per_cyl,
                orig_amt,
            )]
        else:
            # FORMAT 2: total + QxS breakdown
            result = _build_multi(tokens[1:])
            return result if result is not None else [item]
    else:
        # FORMAT 3: all tokens are QxS (no leading total)
        result = _build_multi(tokens)
        return result if result is not None else [item]


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
        stock = get_stock_item_by_name(company_name, product_name, tally.url)
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
        for size, unit, type_code, type_name in variants:
            variant_label = f"{size}{unit}"
            display_name  = f"{base_name} {variant_label} ({type_code})"
            variant_guid  = f"{tally_guid}|{type_code}_{size}{unit}" if tally_guid else ''

            simple_data = dict(common, guid=variant_guid, name=display_name)
            if first_data_json is None:
                first_data_json = simple_data

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
                logger.info(f"[AUTO-FETCH] Product '{display_name}' updated (ID: {existing['id']})")
            else:
                db.insert_product(product_data)
                logger.info(f"[AUTO-FETCH] Product '{display_name}' saved to SQLite")

        return first_data_json

    except Exception as e:
        logger.error(f"[AUTO-FETCH] Failed to fetch product '{product_name}': {e}")
        return None


def _process_invoice_batch(db, tally, company_name, invoices, from_date, to_date, ledger_cache, stock_cache, overall_stats):
    """Process a batch of invoices and save to database."""
    overall_stats['total_fetched'] += len(invoices)

    for invoice in invoices:
        voucher_no = invoice.get('voucher_no', '')
        customer_name = invoice.get('customer_name', '')
        voucher_date = invoice.get('voucher_date', '')

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
            # Rebuild raw_voucher INVENTORY to match expanded items
            orig_to_expanded = {}
            for exp in expanded_items:
                orig_to_expanded.setdefault(exp.get('_orig_name', exp['item_name']), []).append(exp)
            raw_inv = invoice.get('raw_voucher', {}).get('INVENTORY') or []
            new_raw_inv = []
            for raw_item in raw_inv:
                orig_name = (raw_item.get('STOCKITEMNAME') or '').strip()
                if orig_name in orig_to_expanded:
                    for exp in orig_to_expanded[orig_name]:
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
            still_missing = []
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
                else:
                    still_missing.append(mp_name)

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

    # If no dates provided, use Day Book logic (Yesterday to Tomorrow) as requested
    if not from_date and not to_date:
        today_dt = datetime.now()
        from_date = (today_dt - timedelta(days=1)).strftime('%Y%m%d')
        to_date = (today_dt + timedelta(days=1)).strftime('%Y%m%d')
        logger.info(f"Using Day Book fetch logic (Reference: {today_dt.strftime('%Y%m%d')})")
        logger.info(f" -> From Date: {from_date} (Yesterday)")
        logger.info(f" -> To Date:   {to_date} (Tomorrow)")
    else:
        if not from_date:
            from_date = config.INVOICE_FETCH_START_DATE or datetime.now().strftime('%Y%m%d')
        if not to_date:
            to_date = '20991231'

    db = Database(config.SQLITE_DB_PATH)
    tally = TallyClient(config.TALLY_URL)
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
                        _process_invoice_batch(db, tally, company_name, invoices, from_date, to_date, ledger_cache, stock_cache, overall_stats)
                except Exception as b_err:
                    logger.error(f"Batch failed: {b_err}")
                
                curr_start = curr_start + timedelta(days=30)
                import time
                time.sleep(2)

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
